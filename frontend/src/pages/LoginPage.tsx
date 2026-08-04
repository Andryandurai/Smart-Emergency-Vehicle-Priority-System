import { type FormEvent, useEffect, useState } from "react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";

import { auth } from "@/api/endpoints";
import type { DemoAccount } from "@/api/types";
import { landingFor } from "@/app/portals";
import { useAuthStore } from "@/stores/authStore";

export function LoginPage() {
  const status = useAuthStore((state) => state.status);
  const user = useAuthStore((state) => state.user);
  const submitting = useAuthStore((state) => state.submitting);
  const error = useAuthStore((state) => state.error);
  const login = useAuthStore((state) => state.login);

  const navigate = useNavigate();
  const location = useLocation();
  /**
   * Where the guard bounced this visitor from, if it did.
   *
   * Only ever a *suggestion*. It is whatever path was on screen when the
   * session ended, which after a sign-out is the previous user's portal —
   * sign out of the paramedic app at `/p` and this reads `/p` for whoever
   * signs in next. `landingFor` discards it unless it belongs to the portal
   * the new account actually lives in.
   */
  const from = (location.state as { from?: string } | null)?.from ?? null;

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");

  /**
   * Seeded credentials, fetched rather than hardcoded.
   *
   * The old list was gated on `import.meta.env.DEV`, which is false in the
   * built bundle Django serves - so the credentials were invisible in exactly
   * the setup people actually run. The server gates on DEBUG instead and
   * returns nothing in production, which is both the correct check and the
   * one that works here.
   */
  const [accounts, setAccounts] = useState<DemoAccount[]>([]);
  useEffect(() => {
    auth
      .demoAccounts()
      .then((result) => setAccounts(result.accounts))
      .catch(() => setAccounts([]));
  }, []);

  if (status === "authenticated") return <Navigate to={landingFor(user, from)} replace />;

  const submit = async (event: FormEvent): Promise<void> => {
    event.preventDefault();
    if (!(await login(username, password))) return;
    // Read the user the login just stored, rather than this render's closure,
    // which still holds the signed-out state.
    navigate(landingFor(useAuthStore.getState().user, from), { replace: true });
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

        {accounts.length > 0 && (
          <>
            {/* Crew roles get their own cards with names and staff ids -
                a shift is a pairing, so picking *which* driver and *which*
                paramedic is the first thing a tester has to do. */}
            <AccountCard
              title="Driver logins"
              subtitle="Tap a row to fill the form. Drivers select the ambulance and open the shift."
              accounts={accounts.filter((a) => a.is_driver)}
              onPick={fill}
              showName
            />
            <AccountCard
              title="Paramedic logins"
              subtitle="Paramedics accept the driver's sync request and record the assessment."
              accounts={accounts.filter((a) => a.is_paramedic)}
              onPick={fill}
              showName
            />
            {/* Hospital logins belong here rather than behind a picker inside
                the hospital portal: each one opens exactly one ward's board,
                so which ward it is has to be visible *before* signing in. */}
            <AccountCard
              title="Hospital logins"
              subtitle="Each account opens its own hospital's board, ambulances and ward figures."
              accounts={accounts.filter((a) => a.is_hospital)}
              onPick={fill}
              showHospital
            />
            <AccountCard
              title="Other roles"
              subtitle=""
              accounts={accounts.filter(
                (a) => !a.is_paramedic && !a.is_driver && !a.is_hospital,
              )}
              onPick={fill}
            />
            <div className="muted" style={{ fontSize: 11.5, marginTop: 10 }}>
              Shown only while <code>DEBUG</code> is on. Created with{" "}
              <code>manage.py seed_users</code>.
            </div>
          </>
        )}
      </div>
    </div>
  );
}

function AccountCard({
  title,
  subtitle,
  accounts,
  onPick,
  showName = false,
  showHospital = false,
}: {
  title: string;
  subtitle: string;
  accounts: DemoAccount[];
  onPick: (username: string, password: string) => void;
  showName?: boolean;
  /** Show the ward the account opens instead of its holder's name. */
  showHospital?: boolean;
}) {
  if (accounts.length === 0) return null;
  return (
    <div className="card">
      <h3>{title}</h3>
      {subtitle && (
        <p className="muted" style={{ fontSize: 12, marginTop: -4 }}>
          {subtitle}
        </p>
      )}
      <table className="data">
        <thead>
          <tr>
            {showName && <th>Name</th>}
            {showHospital && <th>Hospital</th>}
            <th>Username</th>
            <th>Password</th>
            <th>Role</th>
          </tr>
        </thead>
        <tbody>
          {accounts.map((account) => (
            <tr
              key={account.username}
              onClick={() => onPick(account.username, account.password)}
              style={{ cursor: "pointer" }}
            >
              {showName && (
                <td>
                  <b>{account.name}</b>
                  {(account.staff_id || account.base_station) && (
                    <div className="muted" style={{ fontSize: 10.5 }}>
                      {[account.staff_id, account.base_station]
                        .filter(Boolean)
                        .join(" · ")}
                    </div>
                  )}
                </td>
              )}
              {showHospital && (
                <td>
                  <b>{account.hospital || account.name}</b>
                </td>
              )}
              <td className="mono">{account.username}</td>
              <td className="mono">{account.password}</td>
              <td>{account.role_labels.join(", ")}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
