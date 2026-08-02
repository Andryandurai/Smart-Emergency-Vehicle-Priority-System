# SEVPS Database & Spatial Layer (Phase 3)

## The decision that shapes this phase

The brief said "use GeoDjango where appropriate" and store locations "using spatial
fields". SEVPS does adopt PostGIS — real `geography(Point,4326)` columns, real GiST
indexes, real `ST_DWithin` and KNN ordering — but reaches them through **generated
columns** rather than GeoDjango `PointField`s. That is a deliberate choice with three
concrete reasons:

**1. A generated column cannot drift.** Latitude and longitude already exist on all 13
geo-bearing models and are written by every ingestion path. A mirrored `PointField` needs
sync code or a trigger on every write, and creates a class of bug where the point and the
floats disagree — in a system that routes ambulances, silently wrong coordinates are worse
than no coordinates.

```sql
ALTER TABLE hospitals_hospital ADD COLUMN geom geography(Point,4326)
  GENERATED ALWAYS AS (ST_SetSRID(ST_MakePoint(longitude, latitude),4326)::geography) STORED;
CREATE INDEX hospitals_hospital_geom_gist ON hospitals_hospital USING GIST (geom);
```

PostgreSQL recomputes it from the same source of truth on every write. There is no code
path that can produce an inconsistent value.

**2. GDAL stops being a hard dependency.** The GIS backend requires GDAL on every
developer machine, every CI runner and every container. That is real friction for
indexing gains, and it is why the pilot ran on SQLite in the first place. Opt in with
`SEVPS_USE_GEODJANGO=1` if you want GeoDjango's ORM expressions and have GDAL available;
nothing else changes.

**3. SQLite keeps working.** The same migration graph applies to both backends — the
spatial operations no-op on SQLite. A migration history that diverges by backend is one
nobody can safely roll back.

## How a radius search runs

One entry point, two backends, identical results:

| | PostGIS | SQLite |
|---|---|---|
| Filter | `ST_DWithin(geom, point, radius)` on a GiST index | bounding-box on indexed `latitude`/`longitude` |
| Distance | `ST_Distance` (spheroidal) | haversine in Python |
| Ordering | KNN operator `geom <-> point` | Python sort |
| Complexity | index scan | index range scan + exact test per candidate |

`GeoQuerySet.near()` is unchanged for callers. It still returns a **list** with
`distance_m` set on every object — six call sites depend on that, so the PostGIS path
deliberately does not "improve" it into a lazy queryset. `tests_spatial.py` asserts both
backends agree with a brute-force haversine scan over the same fixtures.

Route and segment polylines get a `geography(LineString,4326)` column too, but it **cannot**
be generated: the JSON stores `[lat, lon]` while PostGIS wants `(x=lon, y=lat)`, and that
axis swap needs an array walk. It is filled by `manage.py backfill_geometry`, which is
idempotent and resumable.

## Migrating SQLite → PostgreSQL

```bash
docker compose up -d db redis          # postgis/postgis + redis
pip install 'psycopg[binary]==3.2.1'
python manage.py migrate_to_postgres --target postgres
```

Seven steps, aborting at the first sign of trouble:

1. Fingerprint the source (`verify_migration --snapshot`)
2. Dump every SEVPS app plus auth/authtoken, with natural keys
3. Check the target is reachable, PostGIS-capable and **empty**
4. `migrate` the schema on the target
5. `loaddata`
6. Fingerprint the target and compare against step 1 — **fails loudly on any mismatch**
7. Backfill LineString geometry

**The source database is never modified.** The dump is kept on disk, so a failure at step
5 or 6 can be replayed by hand.

### Proving nothing was lost

`verify_migration` produces a backend-independent fingerprint: row counts plus an
order-independent checksum per model. Rows are hashed individually and the digests XORed,
so physical row order — which legitimately differs between SQLite and PostgreSQL — cannot
be mistaken for data loss.

```
$ python manage.py verify_migration --compare migration/pre_migration.json
  before: sqlite       3194 rows / 26 models
  after : postgresql   3194 rows / 26 models

  [ok  ] network.RoadSegment        518 ->    518
  [ok  ] dispatch.EmergencyTrip      17 ->     17
  ...
  Verified: every model matched on row count and checksum.
```

On PostGIS it additionally asserts that every generated `geom` agrees with its
`latitude`/`longitude` to 1e-9, and reports rows with no geometry.

Exit status is non-zero on mismatch, so it can gate a deployment script.

## Bug fixed: trip reference race

Phase 1 flagged `EmergencyTrip._generate_reference()` as a read-then-write race. SQLite's
write serialisation hid it; on PostgreSQL with several ASGI workers, two simultaneous
call-outs read the same maximum and the second violates the unique constraint on
`reference`.

Fixed by retrying against the constraint rather than trusting a prior read. A database
sequence was rejected deliberately: it would not reset daily, and it leaves gaps that a
control room reads as lost incidents. The unique constraint is the authority; the loop
re-derives a candidate when it loses, each attempt in its own savepoint so a failed insert
does not poison the caller's transaction.

Covered by a 12-thread concurrent-creation test. That test also required making the SQLite
**test** database file-based (`TEST.NAME` in settings): Django defaults it to shared-cache
memory, which cannot run in WAL mode, so concurrent writers there fail regardless of how
the application is configured — the failure would have been an artefact of the harness.

## JSONField portability

SQLite stores JSON as text, PostgreSQL as `jsonb`. Phase 1 flagged the round-trip risk for
`RoutePlan.steps`/`geometry`, `TrafficSignal.phase_plan` and
`HospitalRecommendationLog.candidates`. `JSONFieldPortabilityTests` asserts the exact
shapes SEVPS stores — nested dicts, floats, booleans, `None` versus `[]` — survive a
round trip with types intact.

One caveat worth knowing: `jsonb` does **not** preserve key order or duplicate keys. No
SEVPS payload depends on either, but code added later must not start to.

## Configuration

```bash
SEVPS_DB_ENGINE=postgres        # sqlite (default) | postgres
SEVPS_ENABLE_POSTGIS=1          # create the extension, columns and indexes
SEVPS_USE_GEODJANGO=0           # 1 = GIS backend (needs GDAL); not required
SEVPS_DB_HOST=127.0.0.1
SEVPS_DB_NAME=sevps
SEVPS_DB_USER=sevps
SEVPS_DB_PASSWORD=sevps
SEVPS_DB_CONN_MAX_AGE=60
```

When `SEVPS_DB_ENGINE=sqlite` and `SEVPS_DB_HOST` is set, a second `postgres` alias is
defined so `migrate_to_postgres` can read the old database and write the new one in a
single run. `GET /api/v1/info/` reports the live spatial backend and PostGIS version, so
which path is active is never a guess.

## Docker

`docker-compose.yml` brings up `postgis/postgis:16-3.4`, `redis:7`, the ASGI server and
the maintenance worker. Redis is not optional in that topology: with the in-memory channel
layer, events raised by the worker never reach a dashboard held open by the web process —
the cross-process limitation found during Phase 1 verification.

The application image deliberately ships **without** GDAL or a build toolchain, which is
only possible because of the generated-column design above.

## What is proven, and what is not

Verified on this machine (SQLite):

- 25 Phase 3 tests, including the 12-thread concurrency regression and JSON round trips
- `verify_migration` fingerprint + self-comparison over 3,194 real rows across 26 models
- `migrate_to_postgres --dump-only` producing a 1.99 MB, 3,394-object dump
- The target guard failing cleanly, with the source database untouched

Authored but **not executed**, because no PostgreSQL server is available here:

- The generated-column DDL and GiST indexes (asserted as SQL strings, not run)
- The `ST_DWithin` / KNN query path (3 tests skip on SQLite and run on PostgreSQL)
- `docker-compose.yml` and `Dockerfile` (Docker is not installed on this machine)

Run `python manage.py test apps.core.tests_spatial` against a PostGIS connection and the
three skipped tests execute, including an `EXPLAIN` assertion that the radius search
genuinely uses the index.
