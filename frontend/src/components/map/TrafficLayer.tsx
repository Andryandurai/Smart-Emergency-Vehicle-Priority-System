/**
 * The road network, drawn imperatively onto a dedicated canvas renderer.
 *
 * Why this is not a `GisLayer`: the seeded network is ~4,000 segments, and the
 * declarative path mounts one React `<Polyline>` *and* one `<Popup>` per
 * feature. A Popup is a React portal, so that is ~8,000 components which React
 * reconciles on every parent render - and the operations page re-renders once
 * a second to keep ETAs honest. The result was a map that dropped frames while
 * panning and appeared to ignore drags entirely.
 *
 * Here the whole network is a single `L.LayerGroup` on its own `L.canvas()`
 * renderer, rebuilt only when the GeoJSON itself changes. Popups are bound
 * through Leaflet rather than React, so panning touches no React state at all.
 *
 * The visible behaviour is identical: same colours, same click-for-detail.
 */
import { useEffect, useMemo, useRef } from "react";
import { useMap } from "react-leaflet";
import L from "leaflet";

import type { GeoCollection } from "./layers";
import { congestionColour, congestionWeight, trafficBand, TRAFFIC_BLOCKED } from "./layers";

interface TrafficLayerProps {
  collection: GeoCollection | null;
  /** Dim the network so a focused route reads on top of it. */
  dimmed?: boolean;
}

export function TrafficLayer({ collection, dimmed = false }: TrafficLayerProps) {
  const map = useMap();
  const groupRef = useRef<L.LayerGroup | null>(null);

  // `padding` keeps a margin of off-screen canvas ready, so a fast drag
  // reveals drawn road rather than blank space that fills in a beat later.
  const renderer = useMemo(() => L.canvas({ padding: 0.4 }), []);

  useEffect(() => {
    const group = L.layerGroup().addTo(map);
    groupRef.current = group;
    return () => {
      group.remove();
      groupRef.current = null;
    };
  }, [map]);

  useEffect(() => {
    const group = groupRef.current;
    if (!group) return;
    group.clearLayers();
    if (!collection) return;

    for (const item of collection.features) {
      if (item.geometry.type !== "LineString") continue;
      const properties = item.properties as Record<string, unknown>;
      const open = properties.is_open !== false;
      const index = Number(properties.congestion_index ?? 0);
      const level = properties.congestion_level;
      const band = trafficBand(index, level);

      const latlngs = (item.geometry.coordinates as [number, number][]).map(
        ([lon, lat]) => [lat, lon] as [number, number],
      );

      const line = L.polyline(latlngs, {
        renderer,
        color: open ? congestionColour(index, level) : TRAFFIC_BLOCKED,
        weight: open ? congestionWeight(index, level) : 5,
        opacity: (open && band === "free" ? 0.5 : 0.85) * (dimmed ? 0.35 : 1),
        ...(open ? {} : { dashArray: "6 5" }),
        lineCap: "round",
        // Clicking a hairline road is fiddly; give the hit area some slack
        // without thickening the drawn line.
        bubblingMouseEvents: false,
      });

      const name = String(properties.name || "Road segment");
      const speed = properties.speed_kmh
        ? `${Math.round(Number(properties.speed_kmh))} km/h`
        : "no live speed";
      line.bindPopup(
        `<b>${escapeHtml(name)}</b><br>${escapeHtml(
          String(properties.congestion_level ?? band),
        )} · ${speed}${open ? "" : "<br><b>CLOSED</b>"}`,
      );
      group.addLayer(line);
    }
  }, [collection, renderer, dimmed]);

  return null;
}

/** Segment names come from OSM imports, so they are not ours to trust. */
function escapeHtml(value: string): string {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}
