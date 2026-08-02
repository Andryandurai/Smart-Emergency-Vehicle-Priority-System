import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { hospitals } from "@/api/endpoints";
import type { Hospital } from "@/api/types";
import { Badge, Empty, ErrorNote } from "@/components/ui";

export function HospitalListPage() {
  const [rows, setRows] = useState<Hospital[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    hospitals
      .list(controller.signal)
      .then(setRows)
      .catch((err: unknown) => {
        if (!(err instanceof DOMException && err.name === "AbortError")) {
          setError(err instanceof Error ? err.message : "Could not load hospitals.");
        }
      });
    return () => controller.abort();
  }, []);

  return (
    <div className="page scroll">
      <div className="page-head">
        <h1>Hospital network</h1>
        <p>
          Open a hospital&rsquo;s Preparedness Dashboard to see inbound patients, ETAs and
          live capacity.
        </p>
      </div>

      <ErrorNote error={error} />

      <div className="grid-3">
        {rows.length === 0 && !error && <Empty>No hospitals registered.</Empty>}
        {rows.map((hospital) => (
          <Link key={hospital.id} className="card link-card" to={`/hospital/${hospital.code}`}>
            <h3>
              {hospital.code}
              {hospital.is_trauma_designated ? " · trauma centre" : ""}
            </h3>
            <div style={{ fontSize: 16, fontWeight: 700 }}>{hospital.name}</div>
            <div className="muted" style={{ fontSize: 12 }}>{hospital.city}</div>
            <div style={{ marginTop: 8 }}>
              {hospital.is_on_diversion ? (
                <Badge tone="bad">On diversion</Badge>
              ) : (
                <Badge tone="ok">Accepting</Badge>
              )}{" "}
              <Badge>{hospital.facility_codes.length} facilities</Badge>
            </div>
          </Link>
        ))}
      </div>
    </div>
  );
}
