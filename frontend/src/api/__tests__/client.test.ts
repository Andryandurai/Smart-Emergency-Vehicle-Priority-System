import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, api, getAccessToken, setAccessToken, setAuthLostHandler } from "@/api/client";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("api client", () => {
  beforeEach(() => {
    setAccessToken(null);
    setAuthLostHandler(null);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("sends the access token as a bearer header", async () => {
    setAccessToken("token-abc");
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ ok: true }));
    vi.stubGlobal("fetch", fetchMock);

    await api.get("/api/v1/auth/me/");

    const headers = fetchMock.mock.calls[0]![1].headers as Record<string, string>;
    expect(headers.Authorization).toBe("Bearer token-abc");
  });

  it("omits the header when signed out", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ ok: true }));
    vi.stubGlobal("fetch", fetchMock);

    await api.get("/api/v1/info/");

    const headers = fetchMock.mock.calls[0]![1].headers as Record<string, string>;
    expect(headers.Authorization).toBeUndefined();
  });

  it("refreshes once on 401 and retries the original request", async () => {
    const fetchMock = vi
      .fn()
      // original request
      .mockResolvedValueOnce(jsonResponse({ detail: "expired" }, 401))
      // refresh
      .mockResolvedValueOnce(jsonResponse({ access: "fresh-token" }))
      // retry
      .mockResolvedValueOnce(jsonResponse({ username: "paramedic" }));
    vi.stubGlobal("fetch", fetchMock);

    const result = await api.get<{ username: string }>("/api/v1/auth/me/");

    expect(result.username).toBe("paramedic");
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(fetchMock.mock.calls[1]![0]).toBe("/api/v1/auth/jwt/refresh/");
    expect(getAccessToken()).toBe("fresh-token");
  });

  it("does not attempt a refresh on 403 - a role gap is not fixable by refreshing", async () => {
    setAccessToken("valid");
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ detail: "forbidden" }, 403));
    vi.stubGlobal("fetch", fetchMock);

    await expect(api.post("/api/v1/dispatch/corridor/tick/", {})).rejects.toThrow(ApiError);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("clears the token and notifies when the refresh fails", async () => {
    setAccessToken("stale");
    const onLost = vi.fn();
    setAuthLostHandler(onLost);

    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValueOnce(jsonResponse({ detail: "expired" }, 401))
        .mockResolvedValueOnce(jsonResponse({ detail: "invalid" }, 401)),
    );

    await expect(api.get("/api/v1/dispatch/trips/live/")).rejects.toThrow(ApiError);
    expect(onLost).toHaveBeenCalledOnce();
    expect(getAccessToken()).toBeNull();
  });

  it("coalesces concurrent 401s into a single refresh", async () => {
    const fetchMock = vi.fn(async (url: string) => {
      if (url === "/api/v1/auth/jwt/refresh/") return jsonResponse({ access: "one-refresh" });
      // Every data call 401s until a token is set, then succeeds.
      return getAccessToken() === "one-refresh"
        ? jsonResponse({ ok: true })
        : jsonResponse({ detail: "expired" }, 401);
    });
    vi.stubGlobal("fetch", fetchMock);

    await Promise.all([
      api.get("/api/v1/fleet/vehicles/live/"),
      api.get("/api/v1/dispatch/trips/live/"),
      api.get("/api/v1/network/events/"),
    ]);

    const refreshCalls = fetchMock.mock.calls.filter(
      (call) => call[0] === "/api/v1/auth/jwt/refresh/",
    );
    expect(refreshCalls).toHaveLength(1);
  });

  it("surfaces DRF field errors on a 400", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse({ override_reason: ["A reason is required."] }, 400),
      ),
    );

    try {
      await api.post("/api/v1/dispatch/trips/1/assess/", {});
      expect.unreachable("should have thrown");
    } catch (error) {
      expect(error).toBeInstanceOf(ApiError);
      const apiError = error as ApiError;
      expect(apiError.status).toBe(400);
      expect(apiError.message).toContain("A reason is required.");
      expect(apiError.fieldErrors.override_reason).toEqual(["A reason is required."]);
    }
  });

  it("marks 401 and 403 as auth errors so pollers can stop", () => {
    expect(new ApiError("x", 401).isAuthError).toBe(true);
    expect(new ApiError("x", 403).isAuthError).toBe(true);
    expect(new ApiError("x", 500).isAuthError).toBe(false);
  });
});
