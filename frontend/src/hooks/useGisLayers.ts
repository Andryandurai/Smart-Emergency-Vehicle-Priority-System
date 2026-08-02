/**
 * Loads GIS layers, remembers which are on, and refreshes the live ones.
 *
 * Refresh cadence is per layer rather than global: the road graph changes when
 * an operator edits it, live vehicle positions change every second, and
 * polling both at the faster rate would move megabytes to learn nothing.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { ApiError, api } from "@/api/client";
import type { BasemapCatalogue, GeoCollection, LayerSpec } from "@/components/map/layers";
import { DEFAULT_ACTIVE_LAYERS } from "@/components/map/layers";

/** Seconds between refreshes, by layer. Absent means load once. */
const REFRESH_SECONDS: Record<string, number> = {
  emergency_vehicles: 3,
  emergency_routes: 6,
  traffic_signals: 8,
  road_closures: 15,
  congestion_heatmap: 30,
  road_network: 60,
  display_boards: 20,
};

const STORAGE_KEY = "sevps.activeLayers";

interface UseGisLayersResult {
  catalogue: LayerSpec[];
  basemaps: BasemapCatalogue | null;
  active: Set<string>;
  data: Record<string, GeoCollection | null>;
  loading: boolean;
  error: string | null;
  toggle: (name: string) => void;
  isActive: (name: string) => boolean;
}

function loadPreference(): Set<string> {
  try {
    const stored = window.localStorage.getItem(STORAGE_KEY);
    if (stored) return new Set(JSON.parse(stored) as string[]);
  } catch {
    // A corrupt preference must not stop the map rendering.
  }
  return new Set(DEFAULT_ACTIVE_LAYERS);
}

export function useGisLayers(): UseGisLayersResult {
  const [catalogue, setCatalogue] = useState<LayerSpec[]>([]);
  const [basemaps, setBasemaps] = useState<BasemapCatalogue | null>(null);
  const [active, setActive] = useState<Set<string>>(loadPreference);
  const [data, setData] = useState<Record<string, GeoCollection | null>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Layers a signed-out user cannot fetch. Remembered so the poller stops
  // rather than retrying a 401 every few seconds for the whole session.
  const forbidden = useRef<Set<string>>(new Set());

  useEffect(() => {
    const controller = new AbortController();
    api
      .get<{ layers: LayerSpec[]; basemaps: BasemapCatalogue }>(
        "/api/v1/network/gis/layers/",
        controller.signal,
      )
      .then((payload) => {
        setCatalogue(payload.layers);
        setBasemaps(payload.basemaps);
      })
      .catch((err: unknown) => {
        if (!(err instanceof DOMException && err.name === "AbortError")) {
          setError(err instanceof Error ? err.message : "Could not load the layer catalogue.");
        }
      })
      .finally(() => setLoading(false));
    return () => controller.abort();
  }, []);

  const fetchLayer = useCallback(async (name: string, signal?: AbortSignal) => {
    if (forbidden.current.has(name)) return;
    try {
      const collection = await api.get<GeoCollection>(
        `/api/v1/network/gis/layers/${name}/`,
        signal,
      );
      setData((previous) => ({ ...previous, [name]: collection }));
    } catch (err) {
      if (err instanceof ApiError && err.isAuthError) {
        forbidden.current.add(name);
        setData((previous) => ({ ...previous, [name]: null }));
        return;
      }
      if (!(err instanceof DOMException && err.name === "AbortError")) {
        setError(err instanceof Error ? err.message : `Could not load layer ${name}.`);
      }
    }
  }, []);

  // One effect per active-set change; each layer gets its own timer so the
  // cadences stay independent.
  useEffect(() => {
    const controller = new AbortController();
    const timers: number[] = [];

    for (const name of active) {
      void fetchLayer(name, controller.signal);
      const seconds = REFRESH_SECONDS[name];
      if (seconds) {
        timers.push(
          window.setInterval(() => void fetchLayer(name, controller.signal), seconds * 1000),
        );
      }
    }

    return () => {
      controller.abort();
      timers.forEach(window.clearInterval);
    };
  }, [active, fetchLayer]);

  const toggle = useCallback((name: string) => {
    setActive((previous) => {
      const next = new Set(previous);
      if (next.has(name)) {
        next.delete(name);
      } else {
        next.add(name);
        // Re-enabling a layer should retry it even if it failed while signed out.
        forbidden.current.delete(name);
      }
      try {
        window.localStorage.setItem(STORAGE_KEY, JSON.stringify([...next]));
      } catch {
        // Private browsing; the map still works, the choice just is not kept.
      }
      return next;
    });
  }, []);

  const isActive = useCallback((name: string) => active.has(name), [active]);

  return useMemo(
    () => ({ catalogue, basemaps, active, data, loading, error, toggle, isActive }),
    [catalogue, basemaps, active, data, loading, error, toggle, isActive],
  );
}
