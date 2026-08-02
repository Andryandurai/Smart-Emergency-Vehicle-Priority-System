/**
 * Driver alert receiver - Layer 4, from the road user's side.
 *
 * Deliberately reachable without an account: a driver must receive an
 * approaching-ambulance warning without holding credentials, which is why
 * /api/v1/alerts/nearby/ and the drivers socket are on the public allowlist.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { useMapEvents } from "react-leaflet";

import { alerts } from "@/api/endpoints";
import type { DriverAlert } from "@/api/types";
import { AlertCircle, Dot, MapCanvas } from "@/components/MapCanvas";
import { Card, ConnectionDot, Empty, fmtTime } from "@/components/ui";
import { usePolling } from "@/hooks/usePolling";
import { useSocket } from "@/hooks/useSocket";

const DEVICE_ID = `web-${Math.random().toString(36).slice(2, 10)}`;

interface LogLine {
  id: number;
  at: Date;
  message: string;
}
let logSeq = 0;

export function DriverPage() {
  const [position, setPosition] = useState<[number, number] | null>(null);
  const [active, setActive] = useState<Record<string, DriverAlert>>({});
  const [cells, setCells] = useState<string[]>([]);
  const [log, setLog] = useState<LogLine[]>([]);

  const positionRef = useRef<[number, number] | null>(null);
  positionRef.current = position;

  const receive = useCallback((alert: DriverAlert) => {
    setActive((prev) => (prev[alert.uuid] ? prev : { ...prev, [alert.uuid]: alert }));
    setLog((prev) =>
      [{ id: ++logSeq, at: new Date(), message: alert.message }, ...prev].slice(0, 40),
    );
  }, []);

  const { status, send } = useSocket("/ws/drivers/", {
    handlers: {
      snapshot: (data) =>
        setCells((data as { subscribed_cells?: string[] }).subscribed_cells ?? []),
      subscribed: (data) => setCells((data as { cells?: string[] }).cells ?? []),
      driver_alert: (data) => receive(data as DriverAlert),
    },
  });

  useEffect(() => {
    if (position && status === "open") {
      send({
        type: "position",
        device_id: DEVICE_ID,
        latitude: position[0],
        longitude: position[1],
      });
    }
  }, [position, status, send]);

  // Polling fallback: an alert raised by the worker process never reaches this
  // socket unless Redis is configured. A road user must not miss a warning.
  usePolling(
    async () => {
      const here = positionRef.current;
      if (!here) return;
      const { alerts: nearby } = await alerts.nearby(here[0], here[1]);
      nearby.forEach(receive);
    },
    3000,
    { immediate: false },
  );

  // Local countdown so the number stays honest between pushes, and expiry
  // clears the banner without waiting for the server.
  useEffect(() => {
    const timer = window.setInterval(() => {
      setActive((prev) => {
        const next: Record<string, DriverAlert> = {};
        for (const [uuid, alert] of Object.entries(prev)) {
          const remaining = alert.eta_seconds - 1;
          if (remaining > -5) next[uuid] = { ...alert, eta_seconds: Math.max(0, remaining) };
        }
        return next;
      });
    }, 1000);
    return () => window.clearInterval(timer);
  }, []);

  const useGps = (): void => {
    if (!navigator.geolocation) return;
    navigator.geolocation.watchPosition(
      (fix) => setPosition([fix.coords.latitude, fix.coords.longitude]),
      undefined,
      { enableHighAccuracy: true },
    );
  };

  const rows = Object.values(active).sort((a, b) => a.eta_seconds - b.eta_seconds);

  return (
    <div className="split">
      <aside className="sidebar">
        <div className="sidebar-head">
          <ConnectionDot status={status} />
        </div>

        <Card title="Your position">
          <p className="muted" style={{ fontSize: 12.5, margin: "0 0 8px" }}>
            Click the map to place yourself, or use real GPS. You subscribe to the geohash
            cells around you and receive warnings only for vehicles approaching your stretch
            of road.
          </p>
          <div className="btn-row">
            <button type="button" className="ghost" onClick={useGps}>
              Use my GPS
            </button>
          </div>
          <div className="muted mono" style={{ fontSize: 12, marginTop: 8 }}>
            {position ? `${position[0].toFixed(5)}, ${position[1].toFixed(5)}` : "not set"}
          </div>
          {cells.length > 0 && (
            <div className="muted" style={{ fontSize: 11, marginTop: 4 }}>
              cells: {cells.join(" ")}
            </div>
          )}
        </Card>

        {rows.length === 0 ? (
          <Card>
            <Empty>No emergency vehicles approaching your location.</Empty>
          </Card>
        ) : (
          rows.map((alert) => (
            <div className="alert-banner" key={alert.uuid}>
              <div className="eta">{Math.round(alert.eta_seconds)}s</div>
              <div className="msg">{alert.instruction}</div>
              <div className="muted" style={{ fontSize: 12.5, marginTop: 6 }}>
                {alert.message}
              </div>
              <div className="muted" style={{ fontSize: 11.5, marginTop: 4 }}>
                approaching from bearing {Math.round(alert.approach_bearing_deg)}&deg; &middot;
                priority level {alert.priority_level}
              </div>
            </div>
          ))
        )}

        <Card title="Alert log">
          <div className="log">
            {log.map((line) => (
              <div className="entry" key={line.id}>
                <span className="t">{fmtTime(line.at)}</span>
                <span>{line.message}</span>
              </div>
            ))}
          </div>
        </Card>
      </aside>

      <MapCanvas>
        <ClickToPlace onPlace={setPosition} />
        {position && (
          <Dot position={position} colour="#4da3ff" radius={8}>
            You
          </Dot>
        )}
        {rows.map((alert) => (
          <AlertCircle
            key={alert.uuid}
            position={[alert.latitude, alert.longitude]}
            radiusM={alert.radius_m}
            message={alert.message}
          />
        ))}
      </MapCanvas>
    </div>
  );
}

function ClickToPlace({ onPlace }: { onPlace: (position: [number, number]) => void }) {
  useMapEvents({ click: (event) => onPlace([event.latlng.lat, event.latlng.lng]) });
  return null;
}
