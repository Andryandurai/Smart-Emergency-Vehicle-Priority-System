/**
 * Upcoming signals along the active route.
 *
 * The one thing a driver on a green corridor needs and cannot get from the
 * road itself: whether the junction 200 m ahead is going to be green when
 * they reach it, or whether the hold failed and they should be covering the
 * brake. Distance leads because that is what the driver is judging against.
 */
import type { Preemption } from "@/api/types";
import { fmtDistance } from "@/components/ui";

export interface UpcomingSignal {
  preemption: Preemption;
  /** Metres from the vehicle's current position, along the route. */
  distanceM: number;
  /** Seconds until the hold takes effect. Negative once it is live. */
  secondsToGreen: number;
}

const STATE_COPY: Record<string, { label: string; tone: string }> = {
  active: { label: "Green corridor ACTIVE", tone: "go" },
  armed: { label: "Clearing cross traffic", tone: "arming" },
  planned: { label: "Green corridor scheduled", tone: "planned" },
  released: { label: "Released - normal timing", tone: "off" },
  failed: { label: "HOLD FAILED - expect red", tone: "fail" },
  cancelled: { label: "Cancelled", tone: "off" },
};

export function SignalStrip({ signals }: { signals: UpcomingSignal[] }) {
  if (signals.length === 0) {
    return (
      <div className="nav-panel">
        <h4>Signals ahead</h4>
        <p className="nav-empty">No signals under priority control on this route.</p>
      </div>
    );
  }

  return (
    <div className="nav-panel">
      <h4>Signals ahead</h4>
      {signals.map(({ preemption, distanceM, secondsToGreen }, index) => {
        const copy = STATE_COPY[preemption.state] ?? STATE_COPY.planned!;
        return (
          <div
            key={preemption.id}
            className={`sig-row ${copy.tone}${index === 0 ? " next" : ""}`}
          >
            <div className="sig-lamp" aria-hidden>
              <i className={preemption.state === "active" ? "on green" : "green"} />
              <i className={preemption.state === "armed" ? "on amber" : "amber"} />
              <i
                className={
                  preemption.state === "failed" || preemption.state === "released"
                    ? "on red"
                    : "red"
                }
              />
            </div>
            <div className="sig-body">
              <div className="sig-dist">{fmtDistance(distanceM)} ahead</div>
              <div className="sig-name">
                {preemption.intersection || preemption.controller_id}
              </div>
              <div className="sig-state">{copy.label}</div>
            </div>
            <div className="sig-eta">
              {preemption.state === "active" ? (
                <span className="go-now">GREEN</span>
              ) : secondsToGreen > 0 ? (
                <>
                  <b>{Math.round(secondsToGreen)}</b>
                  <span>s</span>
                </>
              ) : (
                <span className="muted">—</span>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}
