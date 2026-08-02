# SEVPS — Technologies Used

The problem statement suggested a stack; this records what was actually built with, and
where the implementation diverged and why.

---

## 1. Required — installed by `requirements.txt`

| Layer | Technology | Version | Role in SEVPS |
|---|---|---|---|
| Language | **Python** | 3.11 | Entire backend, AI engine, CLI tooling |
| Web framework | **Django** | 5.0.9 | Project structure, ORM, admin, templates, migrations, auth |
| API | **Django REST Framework** | 3.15.2 | All `/api/v1/` endpoints, serializers, permissions, browsable API |
| API auth | **DRF TokenAuthentication** | (bundled) | Field-device and integration credentials |
| Real time | **Django Channels** | 4.1.0 | WebSocket layer — ops, hospital, vehicle, driver and signal sockets |
| ASGI server | **Daphne** | 4.1.2 | Serves HTTP and WebSockets in one process |
| Graph engine | **NetworkX** | 3.3 | Directed road graph; topology cache the custom time-dependent A\*/Dijkstra search runs over |
| CORS | **django-cors-headers** | 4.4.0 | Cross-origin access for navigation-app and smart-city integrations |
| Config | **python-dotenv** | 1.0.1 | `.env` configuration with working defaults |
| HTTP client | **requests** | 2.32.3 | Signal controller adapter, Overpass/OSM import |
| Database | **SQLite** | bundled | Default store — zero-config pilot deployment |
| Mapping data | **OpenStreetMap** (Overpass API) | — | Real road network import (`import_osm`) |
| Map rendering | **Leaflet** | 1.9.4 | All dashboard maps |
| Basemap tiles | **CARTO dark / OSM** | — | Control-room-appropriate dark basemap |
| Frontend | **HTML5, CSS3, vanilla JavaScript (ES2020)** | — | Server-rendered dashboards, auto-reconnecting WebSocket client |

### Standard library used as infrastructure
`heapq` (priority queue for the route search), `math` (WGS84 geodesy), `dataclasses`
(engine value types), `threading` (graph cache locking), `hashlib` (deterministic
simulation jitter), `statistics` (analytics percentiles).

---

## 2. Optional — implemented, upgrade in place when installed

`GET /api/v1/info/` reports which backend is live for each of these.

| Subsystem | Technology | Enable with |
|---|---|---|
| Spatial database | **PostgreSQL** + **PostGIS** (GeoDjango) | `SEVPS_DB_ENGINE=postgres`, `SEVPS_ENABLE_POSTGIS=1` |
| Multi-worker real time | **Redis** + **channels-redis** | `SEVPS_REDIS_URL=redis://...` |
| Congestion ML | **scikit-learn** (`HistGradientBoostingRegressor`), **NumPy**, **joblib** | `train_congestion_model` + `SEVPS_CONGESTION_MODEL_PATH` |
| Computer vision | **YOLOv8** (ultralytics), **OpenCV** | `SEVPS_CV_MODE=yolo` |
| Task queue | **Celery** + Redis | Production alternative to `sevps_worker` |
| Live traffic enrichment | **Mapbox API**, **Google Maps API**, **OSRM** | `SEVPS_TRAFFIC_PROVIDER`, provider keys |
| Cloud deployment | **AWS / Azure / GCP** | Standard ASGI + Postgres + Redis deployment |

---

## 3. AI / algorithmic components

| Component | Technique | Implementation |
|---|---|---|
| Route optimisation | **Time-dependent A\*** with admissible straight-line/max-speed heuristic | `apps/brain/router.py` |
| Route optimisation (baseline) | **Time-dependent Dijkstra** (uniform-cost) | `apps/brain/router.py` |
| Dynamic replanning | Blocked-edge detection + gain-threshold + cooldown | `apps/brain/rerouting.py` |
| ETA prediction | Per-step predicted-speed integration along the route polyline | `apps/brain/eta.py` |
| Congestion prediction | Live ⊕ learned-profile blend with exponential horizon decay; optional gradient-boosting regressor | `apps/brain/congestion.py` |
| Historical pattern learning | Weekday × hour speed-factor profiles from observation history | `learn_traffic_profiles` |
| Green corridor planning | Queue-aware clearance model, latest-safe arming | `apps/brain/corridor.py` |
| Priority ranking | Interpretable weighted score — severity, imminence, impedance, completion | `apps/brain/priority.py` |
| Hospital recommendation | Rule-based hard filter + weighted multi-factor scoring | `apps/hospitals/` |
| Traffic analysis | YOLOv8 object detection → Greenshields density/speed model | `apps/network/vision.py` |
| Accident hotspots | Stable fixed-grid spatial clustering, severity-weighted | `apps/analytics/services.py` |
| Vehicle projection | Route-polyline projection, dead-reckoning fallback | `apps/brain/eta.py` |
| Geospatial | Haversine, initial bearing, equirectangular segment projection, geohash sharding | `apps/core/geo.py` |

---

## 4. Where the implementation diverged from the suggested stack

**React.js → Django templates + Leaflet + vanilla JS.**
The suggested stack lists React for the frontend. These screens are control-room tools that
must deploy inside a municipal network, often with no Node toolchain and no build step, and
their entire job is to render a live map plus lists driven by a WebSocket feed. Server-
rendered templates deliver that in one process with no build pipeline, and the DRF API is
unchanged — so a React or React Native client can be added on top later without touching
the backend. The API and WebSocket contracts were designed for exactly that.

**FastAPI → Django + DRF.**
The suggested stack lists FastAPI with Node.js optional. Django was chosen because SEVPS
needs an ORM with migrations, a permission model, and an admin interface — the clinical rule
base, hospital capability records and signal configuration all have to be editable by
non-developers (clinical governance, traffic authority staff) without a deployment. DRF
provides the REST surface and Channels the real-time layer, so nothing was given up.

**PostgreSQL/PostGIS → SQLite by default, Postgres/PostGIS supported.**
Coordinates are stored as indexed float pairs with exact distance computed in pure Python
WGS84 maths, so identical behaviour is guaranteed on both backends. GeoDjango would have
required GDAL binaries on every developer and pilot machine, in exchange for indexing gains
that only matter well beyond pilot scale. PostGIS is a one-variable switch when that scale
arrives.

**TensorFlow → scikit-learn (optional).**
The congestion problem is tabular regression on a handful of features, where gradient
boosting is the stronger and far lighter choice; a deep-learning dependency would have added
weight without accuracy. TensorFlow remains the right tool if the CV pipeline is later
retrained in-house rather than using pretrained YOLOv8 weights.

---

## 5. Deployment shape

**Pilot (what this repository runs today)** — two processes, no external services:

```
daphne / runserver  →  SQLite  +  in-memory channel layer
sevps_worker        →  corridor sweep, CV sweep, board expiry, nightly rollups
```

**City scale** — the same code, configuration only:

```
N × Daphne (ASGI)  ─┬─  PostgreSQL + PostGIS
                    ├─  Redis (channel layer)
                    └─  Celery workers + beat  (replacing sevps_worker)
```

Horizontal scaling needs no coordination because the AI engine holds no state: every route,
forecast and recommendation is recomputed from the shared database on demand.
