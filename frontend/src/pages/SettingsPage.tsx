/** Platform settings and live backend status. */
import { useEffect, useState } from "react";

import { auth, service } from "@/api/endpoints";
import type { RoleDescriptor, ServiceInfo } from "@/api/types";
import { Badge, Card, Empty, ErrorNote } from "@/components/ui";
import { NotificationSettings } from "@/components/NotificationSettings";
import { useAuthStore } from "@/stores/authStore";

export function SettingsPage() {
  const user = useAuthStore((state) => state.user);
  const [info, setInfo] = useState<ServiceInfo | null>(null);
  const [roles, setRoles] = useState<RoleDescriptor[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([service.info(), auth.roles()])
      .then(([serviceInfo, roleList]) => {
        setInfo(serviceInfo);
        setRoles(roleList.roles);
      })
      .catch((err: unknown) =>
        setError(err instanceof Error ? err.message : "Could not load platform settings."),
      );
  }, []);

  return (
    <div className="page scroll">
      <div className="page-head">
        <h1>Settings</h1>
        <p>Your account, and which backend each optional subsystem is actually running.</p>
      </div>

      <ErrorNote error={error} />

      <div className="grid-2">
        <Card title="Signed in as">
          {user ? (
            <table className="data">
              <tbody>
                <tr>
                  <th>Username</th>
                  <td className="mono">{user.username}</td>
                </tr>
                <tr>
                  <th>Name</th>
                  <td>{user.name || "-"}</td>
                </tr>
                <tr>
                  <th>Auth method</th>
                  <td className="mono">{user.auth_method ?? "-"}</td>
                </tr>
                <tr>
                  <th>Roles</th>
                  <td>
                    {user.roles.map((role) => (
                      <Badge key={role}>{role}</Badge>
                    ))}
                  </td>
                </tr>
                <tr>
                  <th>Clinical data</th>
                  <td>
                    <Badge tone={user.capabilities.clinical_data ? "ok" : "warn"}>
                      {user.capabilities.clinical_data ? "visible" : "redacted"}
                    </Badge>
                  </td>
                </tr>
                <tr>
                  <th>Traffic control</th>
                  <td>
                    <Badge tone={user.capabilities.traffic_control ? "ok" : "warn"}>
                      {user.capabilities.traffic_control ? "permitted" : "denied"}
                    </Badge>
                  </td>
                </tr>
                <tr>
                  <th>Dispatch control</th>
                  <td>
                    <Badge tone={user.capabilities.dispatch_control ? "ok" : "warn"}>
                      {user.capabilities.dispatch_control ? "permitted" : "denied"}
                    </Badge>
                  </td>
                </tr>
              </tbody>
            </table>
          ) : (
            <Empty>Not signed in.</Empty>
          )}
        </Card>

        <Card title="Live backends">
          {info ? (
            <table className="data">
              <tbody>
                <tr>
                  <th>Database</th>
                  <td className="mono">{info.backends.database}</td>
                </tr>
                <tr>
                  <th>Spatial</th>
                  <td className="mono">
                    {info.backends.spatial.backend}
                    {info.backends.spatial.postgis_version
                      ? ` (PostGIS ${info.backends.spatial.postgis_version})`
                      : ""}
                  </td>
                </tr>
                <tr>
                  <th>Channel layer</th>
                  <td className="mono">{info.backends.channel_layer}</td>
                </tr>
                <tr>
                  <th>Routing</th>
                  <td className="mono">{info.backends.routing_algorithm}</td>
                </tr>
                <tr>
                  <th>Congestion model</th>
                  <td className="mono">{info.backends.congestion_predictor}</td>
                </tr>
                <tr>
                  <th>Computer vision</th>
                  <td className="mono">{info.backends.computer_vision}</td>
                </tr>
                <tr>
                  <th>Traffic provider</th>
                  <td className="mono">{info.backends.traffic_provider}</td>
                </tr>
              </tbody>
            </table>
          ) : (
            <Empty>Loading&hellip;</Empty>
          )}
          {info?.backends.channel_layer === "in-memory" && (
            <div className="muted" style={{ fontSize: 11.5, marginTop: 10 }}>
              The in-memory channel layer is per-process: events raised by{" "}
              <code>sevps_worker</code> or <code>simulate</code> do not reach this browser
              over WebSocket. The console polls as a fallback. Set{" "}
              <code>SEVPS_REDIS_URL</code> for instant cross-process push.
            </div>
          )}
        </Card>
      </div>

      <NotificationSettings />

      <h2>Roles</h2>
      <Card>
        <table className="data">
          <thead>
            <tr>
              <th>Role</th>
              <th>Description</th>
              <th>Clinical</th>
              <th>Traffic</th>
              <th>Dispatch</th>
            </tr>
          </thead>
          <tbody>
            {roles.map((role) => (
              <tr key={role.key}>
                <td className="mono">{role.key}</td>
                <td>{role.description}</td>
                <td>{role.clinical_access ? "yes" : "-"}</td>
                <td>{role.traffic_control ? "yes" : "-"}</td>
                <td>{role.dispatch_control ? "yes" : "-"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>
    </div>
  );
}
