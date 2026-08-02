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

interface LayerControlProps {
  catalogue: LayerSpec[];
  basemaps: BasemapCatalogue | null;
  data: Record<string, GeoCollection | null>;
  isActive: (name: string) => boolean;
  onToggle: (name: string) => void;
  basemapId: string;
  onBasemap: (id: string) => void;
  authenticated: boolean;
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
}: LayerControlProps) {
  const [open, setOpen] = useState(false);

  const overlays = catalogue.filter((layer) => !layer.is_heatmap);
  const heatmaps = catalogue.filter((layer) => layer.is_heatmap);

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
        <span className="layer-title">{layer.title}</span>
        {locked ? (
          <span className="layer-count muted">sign in</span>
        ) : (
          collection && <span className="layer-count">{collection.metadata.count}</span>
        )}
      </label>
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

          <h4>Layers</h4>
          {overlays.map(row)}

          <h4>Heat surfaces</h4>
          {heatmaps.map(row)}

          <div className="layer-note">
            Counts are features currently drawn. Layers marked <em>sign in</em> carry
            operational data and need a role.
          </div>
        </div>
      )}
    </div>
  );
}
