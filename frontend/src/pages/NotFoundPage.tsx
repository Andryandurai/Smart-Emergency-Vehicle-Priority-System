import { Link } from "react-router-dom";

export function NotFoundPage() {
  return (
    <div className="page scroll">
      <div className="card" style={{ maxWidth: 520, margin: "12vh auto", textAlign: "center" }}>
        <h1>Screen not found</h1>
        <p className="muted">That route is not part of the SEVPS console.</p>
        <div className="btn-row" style={{ justifyContent: "center" }}>
          <Link className="signin" to="/">Back to Operations</Link>
        </div>
      </div>
    </div>
  );
}
