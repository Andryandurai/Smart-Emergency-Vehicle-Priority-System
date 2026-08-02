import { type FormEvent, useState } from "react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";

import { useAuthStore } from "@/stores/authStore";

const DEMO_ACCOUNTS = [
  ["admin", "sevps-admin", "Administrator"],
  ["police", "sevps-police", "Traffic Police"],
  ["dispatcher", "sevps-dispatcher", "Emergency Dispatcher"],
  ["paramedic", "sevps-paramedic", "Ambulance Driver"],
  ["hospital", "sevps-hospital", "Hospital Staff"],
] as const;

export function LoginPage() {
  const status = useAuthStore((state) => state.status);
  const submitting = useAuthStore((state) => state.submitting);
  const error = useAuthStore((state) => state.error);
  const login = useAuthStore((state) => state.login);

  const navigate = useNavigate();
  const location = useLocation();
  const from = (location.state as { from?: string } | null)?.from ?? "/";

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");

  if (status === "authenticated") return <Navigate to={from} replace />;

  const submit = async (event: FormEvent): Promise<void> => {
    event.preventDefault();
    if (await login(username, password)) navigate(from, { replace: true });
  };

  const fill = (user: string, pass: string): void => {
    setUsername(user);
    setPassword(pass);
  };

  return (
    <div className="page scroll">
      <div className="login-wrap">
        <div className="page-head" style={{ textAlign: "center" }}>
          <h1>Sign in to SEVPS</h1>
          <p>
            Operational and patient data require a signed-in role. Driver alerts and
            roadside display boards remain open without an account.
          </p>
        </div>

        <form className="card" onSubmit={(event) => void submit(event)}>
          {error && (
            <div className="badge bad" style={{ display: "block", padding: 9, marginBottom: 12 }}>
              {error}
            </div>
          )}

          <label htmlFor="username">Username</label>
          <input
            id="username"
            autoComplete="username"
            autoFocus
            required
            value={username}
            onChange={(event) => setUsername(event.target.value)}
          />

          <label htmlFor="password">Password</label>
          <input
            id="password"
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />

          <div className="btn-row">
            <button type="submit" disabled={submitting}>
              {submitting ? "Signing in…" : "Sign in"}
            </button>
          </div>
        </form>

        {import.meta.env.DEV && (
          <div className="card">
            <h3>Demo accounts</h3>
            <table className="data">
              <thead>
                <tr>
                  <th>Username</th>
                  <th>Password</th>
                  <th>Role</th>
                </tr>
              </thead>
              <tbody>
                {DEMO_ACCOUNTS.map(([user, pass, role]) => (
                  <tr key={user} onClick={() => fill(user, pass)} style={{ cursor: "pointer" }}>
                    <td className="mono">{user}</td>
                    <td className="mono">{pass}</td>
                    <td>{role}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div className="muted" style={{ fontSize: 11.5, marginTop: 10 }}>
              Development build only — never rendered in production. Create these with{" "}
              <code>manage.py seed_users</code>.
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
