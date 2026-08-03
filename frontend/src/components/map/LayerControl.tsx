/**
 * Layer and basemap switcher.
 *
 * Built rather than using Leaflet's own `L.control.layers` because the control
 * needs to show state Leaflet knows nothing about: which layers are heat
 * surfaces, which need a sign-in the current user lacks, and how many features
 * each is currently drawing. An operator asking "why can I not see vehicles"
 * should get the answer from the control, not from devtools.
 */
import { useState } from "react";

import type { BasemapCatalogue, GeoCollection, LayerSpec } from "./layers";

/** A toggle the client owns, not the server layer catalogue. */
export interface ClientLayer {
  name: string;
  title: string;
  description: string;
  count?: number;
  active: boolean;
  onToggle: () => void;
}

interface LayerControlProps {
  catalogue: LayerSpec[];
  basemaps: BasemapCatalogue | null;
  data: Record<string, GeoCollection | null>;
  isActive: (name: string) => boolean;
  onToggle: (name: string) => void;
  basemapId: string;
  onBasemap: (id: string) => void;
  authenticated: boolean;
  /** Extra rows for things drawn from live socket state rather than a GIS
   *  endpoint - currently the driver alert zones. */
  clientLayers?: ClientLayer[];
}

export function LayerControl({
  catalogue,
  basemaps,
  data,
  isActive,
  onToggle,
  basemapId,
  onBasemap,
  authenticated,
  clientLayers = [],
}: LayerControlProps) {
  const [open, setOpen] = useState(false);

  const heatmaps = catalogue.filter((layer) => layer.is_heatmap);
  const overlays = catalogue.filter((layer) => !layer.is_heatmap);

  /**
   * Grouped by the question the operator is asking, not by the backend's
   * layer registry order. "Where is the traffic", "where are my units",
   * "what is in the way" are three different tasks and mixing them into one
   * flat list makes every one of them slower.
   */
  const GROUPS: { title: string; layers: string[] }[] = [
    { title: "Traffic", layers: ["road_network", "road_closures"] },
    { title: "Emergency response", layers: ["emergency_vehicles", "emergency_routes"] },
    { title: "Infrastructure", layers: ["traffic_signals", "hospitals", "display_boards", "cameras"] },
  ];

  const grouped = new Set(GROUPS.flatMap((group) => group.layers));
  const ungrouped = overlays.filter((layer) => !grouped.has(layer.name));

  const row = (layer: LayerSpec) => {
    const locked = !layer.public && !authenticated;
    const collection = data[layer.name];
    return (
      <label
        key={layer.name}
        className={`layer-row${locked ? " locked" : ""}`}
        title={locked ? "Sign in to view this layer" : layer.description}
      >
        <input
          type="checkbox"
          checked={isActive(layer.name)}
          disabled={locked}
          onChange={() => onToggle(layer.name)}
        />
        {/* The swatch is the whole point of a custom control: it shows the
            symbol the layer actually draws, so the checkbox and the map are
            never two separate things to learn. */}
        <LayerSwatch layer={layer.name} />
        <span className="layer-title">{layer.title}</span>
        {locked ? (
          <span className="layer-count muted">sign in</span>
        ) : (
          collection && <span className="layer-count">{collection.metadata.count}</span>
        )}
      </label>
    );
  };

  const clientRow = (layer: ClientLayer) => (
    <label key={layer.name} className="layer-row" title={layer.description}>
      <input type="checkbox" checked={layer.active} onChange={layer.onToggle} />
      <LayerSwatch layer={layer.name} />
      <span className="layer-title">{layer.title}</span>
      {layer.count !== undefined && <span className="layer-count">{layer.count}</span>}
    </label>
  );

  const section = (title: string, names: string[]) => {
    const rows = names
      .map((name) => overlays.find((layer) => layer.name === name))
      .filter((layer): layer is LayerSpec => layer !== undefined);
    if (rows.length === 0) return null;
    return (
      <div key={title}>
        <h4>{title}</h4>
        {rows.map(row)}
      </div>
    );
  };

  return (
    <div className={`layer-control${open ? " open" : ""}`}>
      <button type="button" className="layer-toggle" onClick={() => setOpen((v) => !v)}>
        {open ? "Layers ×" : "Layers"}
      </button>

      {open && (
        <div className="layer-panel">
          {basemaps && (
            <>
              <h4>Basemap</h4>
              <select value={basemapId} onChange={(event) => onBasemap(event.target.value)}>
                {basemaps.providers.map((provider) => (
                  <option key={provider.id} value={provider.id}>
                    {provider.name}
                    {provider.is_traffic ? " (live traffic)" : ""}
                  </option>
                ))}
              </select>
              {!basemaps.mapbox_available && (
                <div className="layer-note">
                  Set <code>SEVPS_MAPBOX_TOKEN</code> to add Mapbox styles and its live
                  traffic tiles.
                </div>
              )}
            </>
          )}

          {GROUPS.map((group) => section(group.title, group.layers))}

          {clientLayers.length > 0 && (
            <>
              <h4>Overlays</h4>
              {clientLayers.map(clientRow)}
            </>
          )}
          {ungrouped.length > 0 && (
            <>
              <h4>Other</h4>
              {ungrouped.map(row)}
            </>
          )}

          {heatmaps.length > 0 && (
            <>
              <h4>Heat surfaces</h4>
              {heatmaps.map(row)}
            </>
          )}

          <div className="layer-note">
            Counts are features currently drawn. Layers marked <em>sign in</em> carry
            operational data and need a role.
          </div>
        </div>
      )}
    </div>
  );
}

/**
 * The symbol a layer draws on the map, at control size.
 *
 * Each is visually distinct in shape as well as colour - a line, a lamp, a
 * pin, a badge, a dashed line - so the control stays readable to an operator
 * who cannot rely on hue.
 */
function LayerSwatch({ layer }: { layer: string }) {
  switch (layer) {
    case "road_network":
      return (
        <span className="lsw lsw-traffic" aria-hidden>
          <i style={{ background: "#4285f4" }} />
          <i style={{ background: "#fbbc04" }} />
          <i style={{ background: "#ea4335" }} />
        </span>
      );
    case "road_closures":
      return <span className="lsw lsw-warn" aria-hidden>!</span>;
    case "alert_zones":
      return <span className="lsw lsw-zone" aria-hidden />;
    case "emergency_vehicles":
      return <span className="lsw lsw-vehicle" aria-hidden>A</span>;
    case "emergency_routes":
      return <span className="lsw lsw-route" aria-hidden />;
    case "traffic_signals":
      return (
        <span className="lsw lsw-signal" aria-hidden>
          <i style={{ background: "#ff3b30" }} />
          <i style={{ background: "#ffab00" }} />
          <i style={{ background: "#34c759" }} />
        </span>
      );
    case "hospitals":
      return <span className="lsw lsw-hospital" aria-hidden>⚕</span>;
    case "display_boards":
      return <span className="lsw lsw-tv" aria-hidden />;
    case "cameras":
      return <span className="lsw lsw-cctv" aria-hidden />;
    default:
      return <span className="lsw lsw-heat" aria-hidden />;
  }
}
