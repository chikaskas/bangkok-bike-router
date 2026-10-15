# Bangkok Bike Router

Finds bike routes across central Bangkok and scores each one on **rider stress**
and **travel time**, so you can pick your own trade-off instead of accepting
whatever is fastest.

Everything runs locally in Python with no third-party runtime: the routing engine
is plain stdlib, and the web UI is static files plus a vendored copy of Leaflet.

## Running it

```bash
./.venv/bin/python src/server.py            # http://127.0.0.1:8000
```

Then open <http://127.0.0.1:8000/>. Options: `--host`, `--port`.

The first start builds the routing graph from `data/bangkok.osm.pbf` and caches
it; later starts load the cache in a few seconds.

### Getting the data

The OSM files are not committed — `data/` is ~500 MB. `scripts/setup_data.sh`
does the whole thing (download, clip, build) and is safe to re-run:

```bash
./scripts/setup_data.sh
```

To do it by hand:

```bash
curl -L -o data/thailand-latest.osm.pbf \
  https://download.geofabrik.de/asia/thailand-latest.osm.pbf

# BBOX in src/config.py, expanded by PAD_DEG (0.012) on every side: the node
# handler reads nodes just outside the box so ways crossing the edge keep
# their geometry. Clipping exactly to BBOX would silently drop them.
osmium extract --bbox 100.3880,13.6280,100.6720,13.8720 \
  -o data/bangkok.osm.pbf data/thailand-latest.osm.pbf

./.venv/bin/python src/build_graph.py --force
```

`--bbox` is `min_lon,min_lat,max_lon,max_lat`. `osmium extract` comes from
`osmium-tool` (`brew install osmium-tool`), separate from the `osmium` Python
package in `requirements.txt`.

The extract this was developed against came from a
[BBBike custom extract](https://extract.bbbike.org/) spanning roughly
100.35,13.48 to 100.8,14.05 — larger than needed. Any extract that covers the
padded box works; `build_graph.py` re-filters to `BBOX` itself.

## Hosting

The repo ships a devcontainer, so the app runs on GitHub Codespaces with no
local setup — **Code → Codespaces → Create codespace on main**, then:

```bash
./scripts/setup_data.sh    # once: fetch the OSM extract and build the graph
python src/server.py       # port 8000 is forwarded automatically
```

GitHub Pages cannot host this. It serves static files only, and routing here is
a Python process holding an 86 MB graph in memory. A browser-only version would
mean porting the router to JavaScript and shipping a compact form of the graph:
it is columnar typed arrays, so downcasting `float64` to `float32` and packing
`edge_lts` into bytes would get the routing payload to roughly 17 MB raw, and
the full-resolution geometry and name tables would want loading lazily.


## How the routing works

**Stress model.** Every edge gets a Level of Traffic Stress (1 = quiet street …
4 = hostile) from its OSM tags: speed limit, lane count, road class, whether a
cycleway exists, surface quality, and whether bikes share the lane with traffic.
LTS is graded finely — half-steps included, so ~11 distinct values in Bangkok —
which is what lets the router tell a quiet soi apart from a service alley that
carries the same road class. LTS 1–2 counts as "low stress", which is what the
**% low-stress** figure in the UI reports.

Because `maxspeed` is tagged on only ~10% of Bangkok ways, road class alone
would treat a leafy quiet tertiary the same as a busy one. So the classifier
also uses a **traffic-volume proxy**: when a way has no bike lane, is a
`tertiary`/`residential`/`unclassified` class, is unnumbered (no `ref`), and is
demonstrably narrow — `lanes` ≤ 2, or a width that implies ≤ 2 lanes — and slow
(≤ 40 km/h), it is demoted from LTS 3 to 2.5. The rule only ever *lowers*
stress, and only fires when lane or width data exists, so ways with no such
tags are left exactly as the class-based model scored them.

**Multi-objective search.** A single generalized cost is minimized:

```
cost = time_seconds × (1 + λ × risk(edge)) + λ × exposure(junction) × risk(junction)
```

The server sweeps λ over `LAMBDA_SWEEP` (0 → 12) with A*, collecting a different
route for each value. λ = 0 is the pure fastest route; large λ buys safety with
time. The results are then reduced to the **Pareto front** on (time, *stress
exposure*) and near-duplicates are collapsed, which is why you typically see
3–5 genuinely different options rather than one per λ. The stress axis is
junction-inclusive (`stress_exposure`), matching the junction term in the cost
above — a route is only discarded as "dominated" if it is worse on *both* time
and total stress, junctions included.

**Junctions are conflict points, not just delays.** A junction is where a rider
actually meets traffic, so each one is charged separately from the road between
them. Signals, stop-controlled nodes and unmarked crossings contribute
*exposure* seconds — how long you sit in conflict with moving traffic — and the
node inherits the risk of its busiest incident road. Turning right (across
oncoming traffic, since Thailand drives on the left) adds more exposure. This
term is charged to **stress only, never to the clock**, so the travel times
reported stay physically real. Junction exposure also counts toward the
comfort and average-stress figures, but never toward distance. Each route
reports both `exposure` (links only) and `stress_exposure` (links +
junctions); the latter is what the Pareto front and duplicate-collapse use.

Time includes turn and junction penalties, and Thailand's left-hand traffic is
accounted for — a right turn crosses oncoming traffic and costs extra.

## Reading the UI

- **The summary line** under the slider is the single readout for the route on
  screen: its name, time, distance, % low-stress, average stress, and a small
  bar showing the distance spent at each stress level.
- **The map** draws the selected route coloured *segment by segment* by stress,
  so you can see exactly which stretch of the trip is the exposed part.
- **The preference slider** is **continuous**. It maps its 0–100 track onto
  λ (the safety/time trade-off weight) logarithmically, and re-plans for the
  exact λ under the handle, so every position is a real route rather than a
  snap to one of a few precomputed ones. The low-λ half of the track gets more
  travel because that is where route choice actually changes. Dragging is
  debounced by 160 ms and a route that is already on screen is not redrawn, so
  dragging does not flicker. Each route is named from the same log boundaries
  (`Fastest`, `Quick`, `Balanced`, `Calm`, `Relaxed`, `Safe`, `Safest`).
- **Two checkboxes keep the view quiet by default:**
  - *Compare alternatives* — off by default, so only one route is on the map.
    Turn it on to draw the other swept routes in grey and show their cards
    (time, distance, stress bar, and biggest stress contributors) in the panel.
    Clicking a card moves the slider onto that route.
  - *Stress legend* — off by default; turn it on for the LTS colour key.
- The base map defaults to **CyclOSM**, which renders bike infrastructure; switch
  to standard or plain tiles from the layer control if you prefer.

## API

| Endpoint | Purpose |
| --- | --- |
| `/api/health` | Liveness check |
| `/api/stats` | Graph size, component count, LTS histogram |
| `/api/search?q=&limit=` | Geocode a place name |
| `/api/route?from=&to=` | Plan routes (names, or `from_lat`/`from_lon` pairs) |

`/api/route` returns a Pareto set, each route with `time_s`, `distance_m`,
`avg_lts`, `comfort_pct`, `safety_score`, `exposure`, `stress_exposure`,
`lts_km`, `junction_count`, `junction_risk_s`, `reasons`, a `key` (stable
12-hex-char hash of the route's edges, so a client can tell when a re-plan
returned the same route), plus `geometry` and a parallel `geometry_lts` array
giving the stress level at each point. The `edges` list is stripped from the
response.

`junction_count` and `junction_risk_s` describe the conflict points on the route
(how many, and how many seconds of traffic exposure they add). They feed
`comfort_pct`, `avg_lts` and `stress_exposure` but never `time_s` or
`distance_m`. `exposure` counts the road links only; `stress_exposure` adds the
junctions and is the axis the Pareto front uses.

Two optional parameters control how much of the trade-off range is swept:

| Parameter | Effect |
| --- | --- |
| `&lam=1.6` | Plan for one exact λ — what the continuous slider uses. Cheapest. |
| `&lams=0,0.8,3,12` | Plan for an explicit list of λ values (max 64). |

Neither → the default `LAMBDA_SWEEP` from `src/config.py`, which is what fills
the comparison cards. λ is clamped to `[MIN_LAMBDA, MAX_LAMBDA]` (0–12);
a non-numeric value returns a 400 with `{"error": "..."}`.

## Rebuilding data

The build caches in two stages, each with its own schema version in
`src/build_graph.py`:

| Cache | Schema | Holds | Cost to rebuild |
| --- | --- | --- | --- |
| `data/extract.pkl` | `EXTRACT_SCHEMA` | Ways/nodes pulled from the OSM PBF | Minutes |
| `data/graph.pkl` | `GRAPH_SCHEMA` | Graph layout + the stress model | Seconds |

Bumping a schema invalidates that stage and everything downstream. So a change
to the stress model only needs `GRAPH_SCHEMA` bumped, which rebuilds the graph
in seconds and reuses the cached extract; bump `EXTRACT_SCHEMA` only when the
set of tags or objects taken from the PBF changes. The graph cache also records
the extract schema it was built from, so re-parsing the extract always
invalidates the graph even if `GRAPH_SCHEMA` is unchanged. To force a full
rebuild:

```bash
rm -f data/extract.pkl data/graph.pkl
./.venv/bin/python src/build_graph.py --force
```

The extract covers `BBOX` in `src/config.py` — central Bangkok, roughly
Rattanakosin and Thonburi in the west to Bang Na in the east. Tunables (speeds,
surface factors, junction exposure and risk, the λ sweep) all live in that file.

## Place search

The gazetteer is built from two sources: named features harvested by
`POI_RULES` in `src/build_graph.py`, and street names derived from the graph
edges. `POI_RULES` is the single biggest lever on coverage — a named feature
whose tags are absent from that table is invisible to search no matter how well
known it is locally. It is evaluated during the PBF parse, so widening it
requires bumping `EXTRACT_SCHEMA` and a full re-parse.

Rules are ordered most-specific first, because the first matching rule wins.

`src/geocode.py` scores candidates by exact / prefix / token-prefix / substring /
fuzzy match, then adds a per-kind boost from `_KIND_BOOST` so useful cycling
endpoints (parks, malls, stations, bike shops) outrank noise (bars, fuel,
parking). Two filters shape the result list:

- A **score floor** (`max(12.0, 0.20 * top)`) drops hopeless tail matches. It is
  deliberately low: fuzzy hits top out near 40, so scaling the floor off an
  exact-match winner (120) would discard the entire fuzzy band and collapse a
  one-letter typo to a single result.
- `_thin` caps any repeated name at two entries, because Bangkok has hundreds
  of 7-Elevens and chain shops.

Street names are collapsed to one weighted centroid per name, so a long road
resolves to its middle rather than to the nearest point on it. There is no
address lookup and no external geocoder fallback: anything not present in the
OSM named set is unfindable.

## Layout

```
src/config.py       tunables and paths
src/build_graph.py  OSM extract -> routing graph + place gazetteer
src/safety.py       OSM tags -> Level of Traffic Stress
src/router.py       A* search, Pareto front, route metrics
src/geocode.py      place-name search and resolution
src/server.py       HTTP API + static file host
web/                the UI (index.html, app.js, style.css, vendored Leaflet)
tools/              developer scripts (compare_graphs.py: before/after route diff)
data/               OSM input and build caches
```
