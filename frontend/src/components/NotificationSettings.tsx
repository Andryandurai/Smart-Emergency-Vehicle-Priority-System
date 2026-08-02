/**
 * Notification preferences and device management, rendered on /settings.
 *
 * Two things it deliberately makes explicit rather than implying:
 *
 *  - **Critical alerts cannot be muted.** A checkbox that silently refuses to
 *    take effect is worse than no checkbox, so the rule is stated in the UI
 *    and matches `NotificationPreference.allows` exactly.
 *  - **A test send reports what actually happened.** Push has a long failure
 *    chain, and "sent" without a delivery count is the sort of reassurance
 *    that gets discovered to be false during an incident.
 */
import { useEffect, useState } from "react";

import { notify } from "@/api/endpoints";
import type {
  NotificationPreferences,
  PushSubscriptionSummary,
  PushTestResult,
} from "@/api/types";
import { Badge, Card, Empty, ErrorNote, fmtTime } from "@/components/ui";
import { usePushNotifications } from "@/hooks/usePushNotifications";

export function NotificationSettings() {
  const push = usePushNotifications(true);
  const [preferences, setPreferences] = useState<NotificationPreferences | null>(null);
  const [devices, setDevices] = useState<PushSubscriptionSummary[]>([]);
  const [configured, setConfigured] = useState(true);
  const [test, setTest] = useState<PushTestResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    Promise.all([notify.preferences(controller.signal), notify.subscriptions(controller.signal)])
      .then(([prefs, subs]) => {
        setPreferences(prefs);
        setDevices(subs.subscriptions);
        setConfigured(subs.push_configured);
      })
      .catch((err: unknown) => {
        if ((err as Error).name !== "AbortError") {
          setError(err instanceof Error ? err.message : "Could not load notification settings.");
        }
      });
    return () => controller.abort();
  }, [push.status.state]);

  const save = async (patch: Partial<NotificationPreferences>) => {
    setSaving(true);
    try {
      setPreferences(await notify.savePreferences(patch));
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save preferences.");
    } finally {
      setSaving(false);
    }
  };

  const toggleCategory = (value: string) => {
    if (!preferences) return;
    const muted = preferences.muted_categories.includes(value)
      ? preferences.muted_categories.filter((item) => item !== value)
      : [...preferences.muted_categories, value];
    void save({ muted_categories: muted });
  };

  return (
    <>
      <h2>Notifications</h2>
      <ErrorNote error={error} />

      <div className="grid-2">
        <Card title="This device">
          <table className="data">
            <tbody>
              <tr>
                <th>Push status</th>
                <td>
                  <Badge tone={push.status.state === "subscribed" ? "ok" : "warn"}>
                    {push.status.state}
                  </Badge>
                </td>
              </tr>
              <tr>
                <th>Detail</th>
                <td>{push.status.detail || "-"}</td>
              </tr>
              <tr>
                <th>Server</th>
                <td>
                  <Badge tone={configured ? "ok" : "warn"}>
                    {configured ? "VAPID configured" : "push not configured"}
                  </Badge>
                </td>
              </tr>
            </tbody>
          </table>

          <div className="row-actions">
            {push.status.state === "subscribed" ? (
              <button type="button" disabled={push.busy} onClick={() => void push.disable()}>
                Turn off push here
              </button>
            ) : (
              <button type="button" disabled={push.busy} onClick={() => void push.enable()}>
                Enable push here
              </button>
            )}
            <button
              type="button"
              disabled={push.status.state !== "subscribed"}
              onClick={() => void notify.sendTest().then(setTest).catch(() => setTest(null))}
            >
              Send test
            </button>
          </div>

          {test && (
            <div className="muted" style={{ fontSize: 11.5, marginTop: 8 }}>
              Attempted {test.attempted}, delivered {test.delivered}, failed {test.failed}
              {test.backend_note ? ` — ${test.backend_note}` : ""}
            </div>
          )}
        </Card>

        <Card title="Registered devices">
          {devices.length === 0 ? (
            <Empty>No devices registered for push on this account.</Empty>
          ) : (
            <table className="data">
              <thead>
                <tr>
                  <th>Device</th>
                  <th>State</th>
                  <th>Last delivery</th>
                </tr>
              </thead>
              <tbody>
                {devices.map((device) => (
                  <tr key={device.id}>
                    <td className="mono" title={device.user_agent}>
                      {device.endpoint_hint}
                    </td>
                    <td>
                      <Badge tone={device.is_healthy ? "ok" : device.is_active ? "warn" : "bad"}>
                        {device.is_active ? (device.is_healthy ? "healthy" : "failing") : "retired"}
                      </Badge>
                    </td>
                    <td className="mono">
                      {device.last_success_at ? fmtTime(device.last_success_at) : "never"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>
      </div>

      <Card title="What you are notified about">
        {!preferences ? (
          <Empty>Loading&hellip;</Empty>
        ) : (
          <>
            <div className="pref-note">{preferences.note}</div>
            <div className="pref-grid">
              {preferences.available_categories.map((category) => {
                const muted = preferences.muted_categories.includes(category.value);
                return (
                  <label key={category.value} className="pref-row">
                    <input
                      type="checkbox"
                      checked={!muted}
                      disabled={saving}
                      onChange={() => toggleCategory(category.value)}
                    />
                    <span>{category.label}</span>
                  </label>
                );
              })}
            </div>

            <div className="pref-row" style={{ marginTop: 12 }}>
              <label>
                <input
                  type="checkbox"
                  checked={preferences.push_enabled}
                  disabled={saving}
                  onChange={(event) => void save({ push_enabled: event.target.checked })}
                />
                <span>Send push for non-critical notifications</span>
              </label>
            </div>

            <div className="pref-row">
              <span>Quiet hours (non-critical only)</span>
              <select
                value={preferences.quiet_hours_start ?? ""}
                onChange={(event) =>
                  void save({
                    quiet_hours_start: event.target.value === "" ? null : Number(event.target.value),
                  })
                }
              >
                <option value="">off</option>
                {HOURS.map((hour) => (
                  <option key={hour} value={hour}>
                    {String(hour).padStart(2, "0")}:00
                  </option>
                ))}
              </select>
              <span>to</span>
              <select
                value={preferences.quiet_hours_end ?? ""}
                onChange={(event) =>
                  void save({
                    quiet_hours_end: event.target.value === "" ? null : Number(event.target.value),
                  })
                }
              >
                <option value="">off</option>
                {HOURS.map((hour) => (
                  <option key={hour} value={hour}>
                    {String(hour).padStart(2, "0")}:00
                  </option>
                ))}
              </select>
            </div>
          </>
        )}
      </Card>
    </>
  );
}

const HOURS = Array.from({ length: 24 }, (_, index) => index);
