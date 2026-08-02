/**
 * The bell in the top bar: unread count, recent notifications, push toggle.
 *
 * Lives in the shell rather than on a page because the point of a notification
 * is that it reaches someone who is looking at something else. A controller
 * reading the analytics screen still needs to know a corridor just failed.
 */
import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";

import type { NotificationRecord } from "@/api/types";
import { usePushNotifications } from "@/hooks/usePushNotifications";
import { fmtTime } from "@/components/ui";

const SEVERITY_TONE: Record<string, string> = {
  critical: "bad",
  warning: "warn",
  success: "ok",
  info: "",
};

export function NotificationCentre({ enabled }: { enabled: boolean }) {
  const push = usePushNotifications(enabled);
  const [open, setOpen] = useState(false);
  const panelRef = useRef<HTMLDivElement | null>(null);
  const navigate = useNavigate();

  // Click-away. A bell panel that stays open while an operator works the map
  // covers the thing they opened it to check.
  useEffect(() => {
    if (!open) return;
    const onClick = (event: MouseEvent) => {
      if (panelRef.current && !panelRef.current.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, [open]);

  if (!enabled) return null;

  const criticalUnread = push.items.some((item) => item.severity === "critical" && !item.is_read);

  const openItem = (item: NotificationRecord) => {
    void push.markRead(item.uuid);
    if (item.link) {
      setOpen(false);
      navigate(item.link);
    }
  };

  return (
    <div className="notif" ref={panelRef}>
      <button
        type="button"
        className={`notif-bell${criticalUnread ? " critical" : ""}`}
        onClick={() => setOpen((value) => !value)}
        aria-label={`Notifications (${push.unread} unread)`}
      >
        <span aria-hidden="true">🔔</span>
        {push.unread > 0 && <span className="notif-badge">{push.unread > 99 ? "99+" : push.unread}</span>}
      </button>

      {open && (
        <div className="notif-panel">
          <div className="notif-head">
            <strong>Notifications</strong>
            {push.unread > 0 && (
              <button type="button" className="linkish" onClick={() => void push.markRead()}>
                Mark all read
              </button>
            )}
          </div>

          <PushBanner
            state={push.status.state}
            detail={push.status.detail}
            busy={push.busy}
            onEnable={() => void push.enable()}
            onDisable={() => void push.disable()}
          />

          <div className="notif-list">
            {push.items.length === 0 ? (
              <div className="notif-empty">
                {push.loading ? "Loading…" : "Nothing in the last 24 hours."}
              </div>
            ) : (
              push.items.map((item) => (
                <button
                  type="button"
                  key={item.uuid}
                  className={`notif-item ${SEVERITY_TONE[item.severity] ?? ""}${
                    item.is_read ? " read" : ""
                  }`}
                  onClick={() => openItem(item)}
                >
                  <div className="notif-item-head">
                    <span className="notif-title">{item.title}</span>
                    <span className="notif-time">{fmtTime(item.created_at)}</span>
                  </div>
                  {item.body && <div className="notif-body">{item.body}</div>}
                  <div className="notif-meta">
                    {item.category_label || item.category}
                    {item.failed_count > 0 && (
                      // Surfaced rather than hidden: if a critical alert
                      // reached nobody, whoever is looking at this needs to
                      // know to pick up a radio.
                      <span className="badge bad"> {item.failed_count} delivery failed</span>
                    )}
                  </div>
                </button>
              ))
            )}
          </div>

          {push.error && <div className="notif-error">History unavailable: {push.error}</div>}
        </div>
      )}
    </div>
  );
}

function PushBanner({
  state,
  detail,
  busy,
  onEnable,
  onDisable,
}: {
  state: string;
  detail: string;
  busy: boolean;
  onEnable: () => void;
  onDisable: () => void;
}) {
  // Each state gets its own wording because each has a different remedy, and
  // "notifications are off" tells someone nothing about how to turn them on.
  if (state === "subscribed") {
    return (
      <div className="notif-push ok">
        <span>Push alerts on for this device.</span>
        <button type="button" className="linkish" disabled={busy} onClick={onDisable}>
          Turn off
        </button>
      </div>
    );
  }
  if (state === "denied") {
    return (
      <div className="notif-push warn">
        Notifications are blocked for this site. Re-enable them in the browser's site
        settings — the page cannot ask again once blocked.
      </div>
    );
  }
  if (state === "unsupported" || state === "insecure") {
    return <div className="notif-push warn">{detail}</div>;
  }
  if (state === "unconfigured") {
    return <div className="notif-push warn">Push is not configured on this server. {detail}</div>;
  }
  return (
    <div className="notif-push">
      <span>Get alerts when this tab is closed.</span>
      <button type="button" className="linkish" disabled={busy} onClick={onEnable}>
        {busy ? "Enabling…" : "Enable push"}
      </button>
    </div>
  );
}
