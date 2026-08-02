/**
 * Authentication state.
 *
 * Deliberately **not** persisted. The access token lives in the API client's
 * module scope and the refresh token is an httpOnly cookie, so persisting user
 * state here would only create a window where the UI claims to be signed in
 * while the server disagrees. `bootstrap()` re-establishes the session on load
 * by attempting a refresh - the cookie is the source of truth.
 */
import { create } from "zustand";

import { ApiError, setAccessToken, setAuthLostHandler } from "@/api/client";
import { auth } from "@/api/endpoints";
import type { CurrentUser, Role } from "@/api/types";

interface AuthState {
  user: CurrentUser | null;
  /** Distinguishes "still checking" from "definitely signed out" so guards
   *  do not redirect to /login during the initial refresh. */
  status: "idle" | "checking" | "authenticated" | "anonymous";
  error: string | null;
  submitting: boolean;

  bootstrap: () => Promise<void>;
  login: (username: string, password: string) => Promise<boolean>;
  logout: () => Promise<void>;
  hasRole: (...roles: Role[]) => boolean;
  can: (capability: keyof CurrentUser["capabilities"]) => boolean;
}

export const useAuthStore = create<AuthState>((set, get) => ({
  user: null,
  status: "idle",
  error: null,
  submitting: false,

  async bootstrap() {
    set({ status: "checking" });

    // A 401 here is the normal signed-out path, so the client's auth-lost
    // handler must not fire mid-bootstrap and cause a redirect loop.
    setAuthLostHandler(null);
    try {
      const user = await auth.me();
      set({ user, status: "authenticated", error: null });
    } catch {
      set({ user: null, status: "anonymous" });
    } finally {
      setAuthLostHandler(() => {
        setAccessToken(null);
        set({ user: null, status: "anonymous" });
      });
    }
  },

  async login(username, password) {
    set({ submitting: true, error: null });
    try {
      const pair = await auth.login(username, password);
      setAccessToken(pair.access);
      // The token response embeds the user, but /auth/me/ re-derives roles
      // from the database - authoritative if a role changed since issue.
      const user = pair.user ?? (await auth.me());
      set({ user, status: "authenticated", submitting: false });
      return true;
    } catch (error) {
      const message =
        error instanceof ApiError && error.status === 401
          ? "Incorrect username or password."
          : error instanceof Error
            ? error.message
            : "Sign-in failed.";
      set({ error: message, submitting: false, status: "anonymous" });
      return false;
    }
  },

  async logout() {
    try {
      await auth.logout();
    } catch {
      // Blacklisting is best-effort: if the network is down the local session
      // must still end, or the operator is stuck signed in.
    }
    setAccessToken(null);
    set({ user: null, status: "anonymous", error: null });
  },

  hasRole(...roles) {
    const user = get().user;
    if (!user) return false;
    if (user.is_superuser) return true;
    return roles.some((role) => user.roles.includes(role));
  },

  can(capability) {
    return Boolean(get().user?.capabilities?.[capability]);
  },
}));
