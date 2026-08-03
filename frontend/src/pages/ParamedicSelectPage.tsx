import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { fleet } from "@/api/endpoints";
import type { VehiclePayload } from "@/api/types";
import { Badge, Empty, ErrorNote, OwnershipTag, levelClass } from "@/components/ui";

export function ParamedicSelectPage() {
  const [vehicles, setVehicles] = useState<VehiclePayload[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    fleet
      .live(controller.signal)
      .then((live) => setVehicles(live.vehicles))
      .catch((err: unknown) => {
        if (!(err instanceof DOMException && err.name === "AbortError")) {
          setError(err instanceof Error ? err.message : "Could not load the fleet.");
        }
      });
    return () => controller.abort();
  }, []);

  return (
    <div className="page scroll">
      <div className="page-head">
        <h1>Paramedic application</h1>
        <p>Select the crew&rsquo;s vehicle to open the on-board view.</p>
      </div>

      <ErrorNote error={error} />

      <div className="grid-3">
        {vehicles.length === 0 && !error && (
          <Empty>
            No vehicles online. Run <code>manage.py seed_demo</code> then{" "}
            <code>manage.py simulate</code>.
          </Empty>
        )}
        {vehicles.map((vehicle) => (
          <Link key={vehicle.callsign} className="card link-card" to={`/paramedic/${vehicle.callsign}`}>
            <h3>{vehicle.vehicle_type_display ?? vehicle.vehicle_type.replaceAll("_", " ")}</h3>
            <div style={{ fontSize: 19, fontWeight: 700 }}>{vehicle.callsign}</div>

            <div className="veh-ident" style={{ marginTop: 8 }}>
              {vehicle.registration && <span className="veh-reg">{vehicle.registration}</span>}
              <OwnershipTag vehicle={vehicle} />
            </div>

            {vehicle.operator && (
              <div className="muted" style={{ fontSize: 11.5, marginTop: 6 }}>
                {vehicle.operator}
              </div>
            )}
            <div className="muted" style={{ fontSize: 12, marginTop: 2 }}>
              {vehicle.status_display ?? vehicle.status}
            </div>

            <div style={{ marginTop: 8 }}>
              <Badge tone={levelClass(vehicle.priority_level) as "l1"}>
                L{vehicle.priority_level}
              </Badge>{" "}
              <Badge>siren {vehicle.siren_mode}</Badge>{" "}
              {vehicle.is_als && <Badge tone="ok">ALS</Badge>}
            </div>
          </Link>
        ))}
      </div>
    </div>
  );
}
