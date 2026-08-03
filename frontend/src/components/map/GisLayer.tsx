/**
 * Renders one GIS layer from the backend's GeoJSON.
 *
 * Every layer goes through this component, so a new layer added server-side
 * is drawable with no new React code - only a styling entry in `layers.ts`.
 *
 * Heatmaps are drawn with Leaflet's own circle primitives rather than a
 * plugin: SEVPS heat surfaces are tens to low hundreds of weighted points
 * (congested segments, clustered hotspots, delayed junctions), and at that
 * size graduated translucent circles read as well as a rasterised heatmap
 * while adding no dependency and staying inspectable - an operator can click
 * a hotspot and see which junction it is.
 */
import type L from "leaflet";
import { memo, useMemo } from "react";
import { CircleMarker, Marker, Polyline, Popup, Tooltip } from "react-leaflet";

import {
  boardIcon,
  cameraIcon,
  disruptionIcon,
  hospitalIcon,
  signalIcon,
} from "@/components/MapCanvas";
import type { GeoCollection } from "./layers";
import { describeFeature, lineStyle, lineToLatLngs, pointStyle, toLatLng } from "./layers";

/** Layers drawn as a glyph marker, and how to build it from the feature. */
const ICON_LAYERS: Record<
  string,
  (properties: Record<string, unknown>, highlighted: boolean) => L.DivIcon
> = {
  road_closures: (p, highlighted) =>
    disruptionIcon({ blocking: Boolean(p.blocks_road), highlighted }),
  display_boards: (p) => boardIcon({ alerting: Boolean(p.is_displaying_alert) }),
  cameras: (p) => cameraIcon({ active: p.is_active !== false }),
};

interface GisLayerProps {
  layer: string;
  collection: GeoCollection | null;
  /** Heat surfaces use graduated radius/opacity from the `weight` property. */
  heatmap?: boolean;
  onSelect?: (properties: Record<string, unknown>) => void;
  /** Feature id to draw emphasised - set when the operator picks one from a
   *  sidebar list, so clicking a disruption finds it on the map. */
  highlightId?: number | string | null;
}

/**
 * `memo` is load-bearing, not decoration: the operations page re-renders every
 * second to keep ETA countdowns honest, and without this every layer would
 * rebuild its features on each tick even though the GeoJSON is unchanged.
 */
export const GisLayer = memo(function GisLayer({
  layer,
  collection,
  heatmap = false,
  onSelect,
  highlightId = null,
}: GisLayerProps) {
  const features = collection?.features ?? [];

  // Splitting once avoids re-testing geometry type on every render, which
  // matters on the 4,000-segment road network.
  const { points, lines } = useMemo(() => {
    const p: typeof features = [];
    const l: typeof features = [];
    for (const item of features) {
      (item.geometry.type === "Point" ? p : l).push(item);
    }
    return { points: p, lines: l };
  }, [features]);

  if (!collection) return null;

  return (
    <>
      {lines.map((item, index) => (
        <Polyline
          key={`${layer}-line-${item.id ?? index}`}
          positions={lineToLatLngs(item.geometry.coordinates as [number, number][])}
          pathOptions={lineStyle(layer, item.properties)}
          eventHandlers={onSelect ? { click: () => onSelect(item.properties) } : undefined}
        >
          <Popup>
            <span
              // Popup content is built from our own typed properties, never
              // from user input, so this is safe and keeps the markup simple.
              dangerouslySetInnerHTML={{ __html: describeFeature(layer, item.properties) }}
            />
          </Popup>
        </Polyline>
      ))}

      {points.map((item, index) => {
        const coordinates = item.geometry.coordinates as [number, number];
        if (heatmap) {
          const weight = Math.max(0, Math.min(1, Number(item.properties.weight) || 0));
          return (
            <CircleMarker
              key={`${layer}-heat-${item.id ?? index}`}
              center={toLatLng(coordinates)}
              // Radius in pixels, so the surface stays legible at any zoom.
              radius={6 + 22 * weight}
              pathOptions={{
                color: "transparent",
                fillColor: heatColour(weight),
                fillOpacity: 0.15 + 0.45 * weight,
              }}
            >
              <Popup>
                <b>{String(item.properties.name ?? item.properties.label ?? layer)}</b>
                <br />
                intensity {Math.round(weight * 100)}%
              </Popup>
            </CircleMarker>
          );
        }

        const key = `${layer}-point-${item.id ?? index}`;
        const properties = item.properties;
        const handlers = onSelect ? { click: () => onSelect(properties) } : undefined;

        // Most layers earn a real icon rather than a dot: a coloured circle
        // cannot say "hazard", "screen" or "camera", and on a map that already
        // spends red, amber and blue on traffic, colour alone was ambiguous.
        const iconFor = ICON_LAYERS[layer];
        if (iconFor) {
          const highlighted = highlightId != null && item.id === highlightId;
          return (
            <Marker
              key={key}
              position={toLatLng(coordinates)}
              icon={iconFor(properties, highlighted)}
              zIndexOffset={highlighted ? 900 : 300}
              eventHandlers={handlers}
            >
              <Tooltip direction="top" offset={[0, -20]} opacity={1} className="veh-tip">
                <span
                  dangerouslySetInnerHTML={{ __html: describeFeature(layer, properties) }}
                />
              </Tooltip>
              <Popup>
                <span
                  dangerouslySetInnerHTML={{ __html: describeFeature(layer, properties) }}
                />
              </Popup>
            </Marker>
          );
        }

        if (layer === "hospitals") {
          return (
            <Marker
              key={key}
              position={toLatLng(coordinates)}
              icon={hospitalIcon({
                onDiversion: Boolean(properties.is_on_diversion),
                isTrauma: Boolean(properties.is_trauma_designated),
              })}
              zIndexOffset={500}
              eventHandlers={handlers}
            >
              <Tooltip direction="top" offset={[0, -34]} opacity={1} className="veh-tip">
                <span
                  dangerouslySetInnerHTML={{ __html: describeFeature(layer, properties) }}
                />
              </Tooltip>
              <Popup>
                <span
                  dangerouslySetInnerHTML={{ __html: describeFeature(layer, properties) }}
                />
              </Popup>
            </Marker>
          );
        }

        if (layer === "traffic_signals") {
          return (
            <Marker
              key={key}
              position={toLatLng(coordinates)}
              icon={signalIcon(String(properties.current_phase ?? "off"), {
                preempted: Boolean(properties.is_preempted),
                online: properties.is_online !== false,
              })}
              zIndexOffset={200}
              eventHandlers={handlers}
            >
              <Popup>
                <span
                  dangerouslySetInnerHTML={{ __html: describeFeature(layer, properties) }}
                />
              </Popup>
            </Marker>
          );
        }

        const style = pointStyle(layer, properties);
        return (
          <CircleMarker
            key={key}
            center={toLatLng(coordinates)}
            radius={style.radius}
            pathOptions={{
              color: style.colour,
              fillColor: style.colour,
              fillOpacity: 0.9,
              weight: 1,
            }}
            eventHandlers={handlers}
          >
            <Popup>
              <span
                dangerouslySetInnerHTML={{ __html: describeFeature(layer, properties) }}
              />
            </Popup>
          </CircleMarker>
        );
      })}
    </>
  );
});

/** Cool-to-hot ramp. Kept monotonic in luminance so it survives greyscale. */
function heatColour(weight: number): string {
  if (weight >= 0.8) return "#e74c3c";
  if (weight >= 0.6) return "#e67e22";
  if (weight >= 0.4) return "#f1c40f";
  if (weight >= 0.2) return "#9acd32";
  return "#4da3ff";
}
