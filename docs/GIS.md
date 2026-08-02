# GIS & Mapping (Phase 8)

## Why this phase existed

Before Phase 8 the map was assembled by hand. `OperationsPage` fetched
`/api/v1/network/segments/`, `HospitalPage` fetched hospitals, the paramedic
screen drew its own route polyline. Each screen decided independently what a
hospital marker looked like, which meant three different answers to "is this
hospital on diversion" and no way to add a layer without editing three files.

Phase 8 replaces that with **one declarative layer registry** on the server and
**one renderer** on the client. Adding a layer is now a server-side change only.

Nothing was removed. `SegmentsLayer`, `RouteLine` and `Dot` still exist in
`MapCanvas.tsx` and are still used by the paramedic and hospital screens; the
`/network/segments/` endpoint is unchanged and still serves them.

---

## The layer registry

`apps/network/gis.py` holds one `LayerSpec` per layer and a builder function
that returns a GeoJSON `FeatureCollection`.

| Layer | Geometry | Access | Refresh | What it shows |
|---|---|---|---|---|
| `road_network` | LineString | public | 60 s | Roads coloured by live congestion index |
| `hospitals` | Point | public | 30 s | Sites, capability set, free beds, diversion state |
| `traffic_signals` | Point | public | 10 s | Junctions and whether they are currently held green |
| `road_closures` | Point | public | 20 s | Blocking road events — the public reason to avoid a street |
| `emergency_routes` | LineString | role | 5 s | Planned corridors for active trips |
| `emergency_vehicles` | Point | role | 3 s | Live fleet with priority level and siren state |
| `display_boards` | Point | role | 30 s | Variable-message signs and their current text |
| `cameras` | Point | role | 30 s | CV camera sites and last analysis verdict |
| `congestion_heatmap` | Point | public | 30 s | Congested segment midpoints, weighted |
| `accident_heatmap` | Point | role | 300 s | Clustered historical incident hotspots |
| `delay_heatmap` | Point | role | 120 s | Junctions where corridors historically lose time |

**Access = `public`** means the layer is in the `PUBLIC_READ_ENDPOINTS`
allowlist in `apps/core/api_policy.py` with a written reason. Road geometry,
hospital locations and closures are public infrastructure — a citizen route
planner is a legitimate consumer. **Access = `role`** means any authenticated
SEVPS role; live vehicle positions and incident history are not public.

`test_catalogue_declares_permissions_matching_enforcement` checks the
advertised `public` flag against what the endpoint actually does, so the
catalogue cannot drift from enforcement.

### Endpoints

```
GET /api/v1/network/gis/layers/           # catalogue: name, title, geometry, refresh, public
GET /api/v1/network/gis/layers/<name>/    # one FeatureCollection
GET /api/v1/network/gis/basemaps/         # tile providers available to this deployment
```

Every collection carries a `metadata` block:

```json
{
  "type": "FeatureCollection",
  "features": [ ... ],
  "metadata": {
    "layer": "road_network",
    "count": 3812,
    "generated_at": "2026-08-02T09:14:22+05:30",
    "truncated": false
  }
}
```

`truncated` is not decoration. The seeded Chennai network is ~4,000 polylines;
a client that silently receives 2,000 of them draws a map with holes in it and
nothing says so. Heat layers additionally declare `weight_field` so the
renderer does not have to guess which property drives intensity.

---

## Coordinate order — the bug this phase was most likely to ship

GeoJSON is `[longitude, latitude]`. Leaflet is `[latitude, longitude]`. In
Chennai (13.06 N, 80.25 E) a swap puts every feature at 80 N 13 E — in the
Norwegian Sea. The map does not error; it renders empty, which reads as "no
data yet" rather than "wrong".

The rule: **the server emits GeoJSON order, and `toLatLng()` in
`frontend/src/components/map/layers.ts` is the only place that flips.** Four
tests hold this — `test_points_are_lon_lat_not_lat_lon`,
`test_linestrings_are_lon_lat`, `test_the_conversion_helper_flips_exactly_once`
and the Vitest equivalent.

Django models store `latitude`/`longitude` as separate floats and
`RoadSegment.geometry` as `[lat, lon]` pairs (the existing convention, kept
unchanged). `latlon_pairs_to_geojson()` does the conversion at the boundary.

---

## Heatmaps without a plugin

`leaflet.heat` was evaluated and not adopted. It renders a canvas blob with no
hit-testing, so an operator cannot click a hot cell to ask *why* — and "which
junction is this" is the only question a delay heatmap is asked. It also has no
React 19 bindings and would have needed an imperative escape hatch.

Instead heat surfaces are graduated `CircleMarker`s: radius and opacity scale
with `weight`, and each one keeps its popup. Slightly less pretty at city zoom,
considerably more useful, and no dependency.

Two deliberate choices in the builders:

- `congestion_heatmap` **omits free-flowing roads**. A heat surface covering
  every segment is a solid rectangle, which is not information.
- `accident_heatmap` reads the pre-clustered `analytics.Hotspot` table rather
  than raw incidents, so one junction is one hot cell, not forty overlapping
  ones.

---

## Basemaps

| Provider | Key needed | Notes |
|---|---|---|
| CARTO Dark / Light (OSM data) | no | **Default.** |
| OpenStreetMap standard | no | |
| Mapbox Streets | `SEVPS_MAPBOX_TOKEN` | appears only when set |
| Mapbox Traffic | `SEVPS_MAPBOX_TOKEN` | vendor live-traffic tiles |

The default basemap **must not require a key**. An emergency platform that
cannot draw a map because a tile contract lapsed has failed at something more
basic than mapping. Mapbox is an enhancement layered on top, not a dependency.

### Google Maps: configured, deliberately not rendered

`SEVPS_GOOGLE_MAPS_KEY` is read and reported in
`basemap_providers()["google_maps_available"]`, but Google is not offered as a
tile provider. The brief listed it as optional, and here is the recommendation
asked for:

**Do not integrate it as a basemap.** Google's tile terms require the Google
Maps JS API rather than raw `TileLayer` access, which means shipping a second
mapping runtime alongside Leaflet for a rendering result OSM already provides.
The genuine Google advantage is its **traffic and directions data**, not its
tiles — and SEVPS derives traffic from its own sensor, CV and telemetry
pipeline, which knows about held signals and closed roads that Google does not.

Where Google *would* add something is as a **fallback traffic source** for
roads SEVPS has no sensor coverage on. That belongs behind the existing
`SEVPS_TRAFFIC_PROVIDER` setting as a data adapter, not as a basemap. The key
is read and surfaced so that work needs no settings change when it happens.

---

## Frontend

```
src/components/map/layers.ts       types, toLatLng, styling, describeFeature
src/components/map/GisLayer.tsx    renders any layer from its spec
src/components/map/LayerControl.tsx  layer + basemap switcher
src/hooks/useGisLayers.ts          catalogue fetch, per-layer polling, preferences
```

`useGisLayers` polls each layer at **its own** `refresh_seconds`. Vehicles at
3 s and the accident heatmap at 300 s in one timer would mean either stale
vehicles or 100× unnecessary heatmap queries.

Two behaviours worth knowing:

- **A 403 stops that layer's polling.** Without the `forbidden` ref, an
  anonymous visitor with `emergency_vehicles` enabled generates a 401 every
  three seconds forever.
- **Layer choices persist to `localStorage`.** A controller who turns off the
  road network to see vehicles clearly should not have to do it again after a
  refresh.

`LayerControl` is hand-built rather than Leaflet's `L.control.layers` because
it shows state Leaflet has no concept of: feature counts, which layers are heat
surfaces, and which are locked behind a sign-in. An operator asking "why can't
I see vehicles" gets the answer from the control instead of from devtools.

---

## What changed in existing files

| File | Change | Risk |
|---|---|---|
| `sevps/api_urls.py` | three new GIS routes | none — additive |
| `apps/core/api_policy.py` | four public layers allowlisted with reasons | none — audited |
| `frontend/src/components/MapCanvas.tsx` | `basemap` prop, `FALLBACK_BASEMAP` | none — prop is optional, omitting it keeps the previous tiles |
| `frontend/src/pages/OperationsPage.tsx` | hand-built layers → `GisLayer` | behavioural: the ops map now honours layer toggles |

`HospitalPage` and `ParamedicPage` were **not** migrated. They render one
focused thing each — a single hospital's catchment, one crew's route — and the
layer system is built for the multi-layer operational picture. Forcing them
through it would add indirection without removing any duplication.

---

## Verification

```
python manage.py test apps.network.tests_gis     # 27 tests
python manage.py test apps                       # 279 tests, OK
cd frontend && npx vitest run                    # 19 tests
cd frontend && npx tsc --noEmit && npx vite build
```
