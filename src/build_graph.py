"""Build a routable, safety-scored bicycle graph from an OSM extract.

Two stages, each cached to disk:

  1. ``build_extract`` reads the PBF and keeps only what we need: node
     coordinates, junction tags, named POIs and the tags of routable ways.
  2. ``build_graph`` compresses those ways into a routing graph whose vertices
     are real junctions, scoring every edge for traffic stress.

The PBF is read twice (nodes, then ways) so the result never depends on the
order elements happen to appear in the file.

Run directly to (re)build and print a summary::

    python src/build_graph.py [--force]
"""

from __future__ import annotations

import argparse
import math
import pickle
import sys
import time
from array import array
from collections import Counter

import osmium

from config import (
    BBOX,
    DATA_DIR,
    DEFAULT_FERRY_SPEED_KMH,
    EXTRACT_CACHE,
    GRAPH_CACHE,
)
from safety import (
    access_penalty_s,
    bicycle_speed_kmh,
    level_of_traffic_stress,
    risk_from_lts,
)

# Bumped whenever the *extraction* changes (which tags/objects are pulled out of
# the PBF), so a stale extract is rebuilt. Keep this stable when only the safety
# model changes: re-parsing the PBF costs minutes, rebuilding the graph seconds.
EXTRACT_SCHEMA = 3

# Bumped whenever the *graph layout* or the safety model changes, so a stale
# graph is rebuilt from the (still valid) extract.
GRAPH_SCHEMA = 5

# Coordinates are cached for a slightly larger area than the routing bbox so
# that roads crossing the boundary stay connected instead of dead-ending.
PAD_DEG = 0.012

# Named features indexed for the place-search box.
# Named-feature harvest rules. `_poi_kind` returns the FIRST rule that matches,
# so specific kinds are listed before generic ones. This table is the single
# biggest lever on how many places the geocoder can offer: a named feature whose
# tags are absent here is invisible to search no matter how well known it is.
POI_RULES = (
    # --- rail and air ---
    ("railway", ("station", "halt", "tram_stop"), "transit station"),
    ("railway", ("subway_entrance",), "transit entrance"),
    ("aeroway", ("aerodrome", "terminal", "helipad", "heliport"), "airport"),
    # --- education and health ---
    ("amenity", ("university", "college"), "university"),
    ("amenity", ("school", "kindergarten"), "school"),
    ("amenity", ("hospital",), "hospital"),
    ("amenity", ("clinic", "doctors", "dentist", "veterinary"), "clinic"),
    ("amenity", ("pharmacy",), "pharmacy"),
    ("healthcare", ("hospital", "clinic", "doctor", "dentist", "centre", "pharmacy"), "clinic"),
    # --- civic ---
    ("amenity", ("police", "fire_station", "post_office", "townhall", "courthouse",
                 "community_centre", "library", "embassy", "prison",
                 "ranger_station"), "civic"),
    ("office", ("government", "diplomatic", "administrative", "municipality"), "government office"),
    ("office", ("company", "it", "estate_agent", "insurance", "lawyer", "ngo",
                "educational_institution", "research", "coworking", "consulting",
                "financial", "telecommunication", "newspaper"), "office"),
    # --- shops ---
    ("shop", ("mall", "department_store"), "mall"),
    ("shop", ("bicycle", "bicycle_rental", "bicycle_repair_station"), "bike shop"),
    ("shop", ("supermarket", "convenience", "variety_store", "wholesale", "greengrocer",
              "bakery", "butcher", "seafood", "deli", "cheese", "pastry",
              "confectionery", "coffee", "tea", "beverages"), "shop"),
    ("shop", ("doityourself", "hardware", "electronics", "mobile_phone", "computer",
              "furniture", "clothes", "shoes", "jewelry", "optician", "sports",
              "books", "stationery", "toys", "pet", "travel_agency", "car_repair",
              "car", "garden_centre", "florist", "gift", "art", "music", "photo",
              "beauty", "hairdresser", "laundry", "dry_cleaning", "tailor",
              "fabric", "paint", "kitchen", "appliance", "bed", "carpet",
              "cosmetics", "chemist", "medical_supply", "massage", "copyshop",
              "printing", "photo_studio", "interior_decoration"), "shop"),
    # --- food, drink, money ---
    ("amenity", ("restaurant", "fast_food", "food_court"), "restaurant"),
    ("amenity", ("cafe", "ice_cream"), "cafe"),
    ("amenity", ("bar", "pub", "biergarten", "nightclub"), "bar"),
    ("amenity", ("bank", "atm", "bureau_de_change"), "bank"),
    ("amenity", ("fuel", "charging_station", "car_wash"), "fuel"),
    ("amenity", ("marketplace",), "market"),
    # --- transport ---
    ("amenity", ("bus_station",), "bus station"),
    ("highway", ("bus_stop",), "bus stop"),
    ("amenity", ("ferry_terminal",), "ferry pier"),
    ("amenity", ("bicycle_parking", "bicycle_rental", "bicycle_repair_station"), "bike parking"),
    ("amenity", ("parking", "parking_entrance", "motorcycle_parking"), "parking"),
    ("amenity", ("taxi",), "taxi"),
    # --- worship and heritage ---
    ("amenity", ("place_of_worship",), "temple"),
    ("historic", ("monument", "memorial", "ruins", "castle", "fort",
                  "archaeological_site", "wayside_shrine", "city_gate",
                  "building", "tomb", "manor", "yes"), "historic"),
    # --- leisure and nature ---
    ("leisure", ("park", "garden", "nature_reserve", "dog_park", "playground",
                 "common", "recreation_ground", "bird_hide"), "park"),
    ("leisure", ("fitness_centre", "sports_centre", "swimming_pool", "stadium",
                 "golf_course", "water_park", "marina", "track", "pitch",
                 "sports_hall", "ice_rink", "dance"), "sport"),
    ("leisure", ("cinema", "theatre", "arts_centre", "bowling_alley",
                 "amusement_arcade", "casino"), "entertainment"),
    ("tourism", ("attraction", "museum", "viewpoint", "gallery", "theme_park",
                 "zoo", "artwork", "aquarium", "information", "picnic_site"), "attraction"),
    ("tourism", ("hotel", "hostel", "guest_house", "motel", "apartment",
                 "chalet", "resort", "camp_site", "caravan_site"), "hotel"),
    # --- water, land and buildings ---
    ("waterway", ("canal", "river", "dock", "boatyard", "stream", "dam"), "canal"),
    ("natural", ("water", "peak", "beach", "wood", "wetland", "bay", "spring"), "nature"),
    ("residential", ("apartments", "house", "dormitory", "gated", "terrace",
                     "detached", "semidetached_house", "bungalow"), "residential"),
    ("landuse", ("residential", "retail", "commercial", "industrial", "quarry",
                 "cemetery", "farmyard", "allotments", "vineyard", "orchard",
                 "military", "religious", "education", "recreation_ground"), "landuse"),
    ("building", ("yes", "commercial", "apartments", "residential", "office",
                  "retail", "hotel", "university", "school", "hospital",
                  "industrial", "warehouse", "civic", "public", "government",
                  "dormitory", "church", "temple", "mosque", "train_station",
                  "transportation", "supermarket", "mall", "kindergarten",
                  "college", "stadium", "sports_centre", "roof", "house",
                  "detached", "terrace", "bungalow", "farm", "hangar",
                  "garage", "garages", "service"), "building"),
    # --- man made landmarks ---
    ("man_made", ("tower", "water_tower", "lighthouse", "pier", "bridge",
                  "mast", "observatory", "works", "storage_tank", "chimney",
                  "water_works", "breakwater", "groyne"), "landmark"),
    # --- administrative places ---
    ("place", ("suburb", "neighbourhood", "quarter", "city", "town", "village",
               "hamlet", "locality", "borough", "island", "farm", "allotments"), "district"),
)

# Node flags packed into one small int.
FLAG_SIGNAL = 1
FLAG_CROSSING = 2
FLAG_STOP = 4


# --------------------------------------------------------------------------
# geometry helpers
# --------------------------------------------------------------------------
def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial compass bearing from point 1 to point 2, in degrees [0, 360)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


_SCALE = 10_000_000  # 1e-7 degrees, ~1.1 cm at the equator
_LON_SPAN = 360 * _SCALE  # must exceed any shifted longitude, else fields collide


def _pack(lat: float, lon: float) -> int:
    lat_i = round((lat + 90.0) * _SCALE)
    lon_i = round((lon + 180.0) * _SCALE)
    return lat_i * _LON_SPAN + lon_i


def _unpack(v: int) -> tuple[float, float]:
    lat_i, lon_i = divmod(v, _LON_SPAN)
    return lat_i / _SCALE - 90.0, lon_i / _SCALE - 180.0


def _bounds(bbox, pad=0.0):
    s, w, n, e = bbox
    return (s - pad, w - pad, n + pad, e + pad)


# --------------------------------------------------------------------------
# stage 1: extract
# --------------------------------------------------------------------------
def _poi_kind(tags: dict) -> str | None:
    for key, values, label in POI_RULES:
        if tags.get(key) in values:
            return label
    return None


_ALIAS_KEYS = (
    "short_name", "short_name:en",
    "alt_name", "alt_name:en",
    "official_name", "official_name:en",
    "loc_name", "loc_name:en",
    "old_name", "old_name:en",
)

# Display preference: what locals actually call the place, shortest first.
_COMMON_KEYS = ("short_name:en", "loc_name:en", "short_name", "loc_name")


def _common_name(tags: dict, *primary: str | None) -> str | None:
    """The everyday name of a feature, when it differs from its official one.

    Wat Pho is tagged `name=วัดพระเชตุพนวิมลมังคลาราม...` with the name people
    use living in `loc_name`; showing the ceremonial name helps nobody. Thai
    abbreviations such as Chulalongkorn's `short_name=จุฬา` are skipped so the
    display name stays readable to the people using the English UI.
    """
    seen = {p for p in primary if p}
    for key in _COMMON_KEYS:
        value = tags.get(key)
        if value and value not in seen and any(ch.isascii() and ch.isalpha() for ch in value):
            return value
    return None


def _alias_names(tags: dict, *primary: str | None) -> tuple[str, ...]:
    """Secondary names OSM keeps alongside `name`/`name:en`.

    Landmarks such as Wat Pho are tagged with a long ceremonial primary name,
    while the name people actually type ("Wat Pho") sits in loc_name/short_name.
    """
    seen = {p for p in primary if p}
    out: list[str] = []
    for key in _ALIAS_KEYS:
        value = tags.get(key)
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return tuple(out)


def _is_routable_way(tags: dict) -> bool:
    if tags.get("route") == "ferry":
        return True
    if "highway" not in tags:
        return False
    lts, _ = level_of_traffic_stress(tags)
    return lts is not None


class _NodeHandler(osmium.SimpleHandler):
    def __init__(self, bounds):
        super().__init__()
        self.bounds = bounds
        self.coords: dict[int, int] = {}
        self.node_tags: dict[int, int] = {}
        self.poi_nodes: list[tuple[int, str, str, str | None, tuple[str, ...], str]] = []

    def node(self, n):
        lat, lon = n.location.lat, n.location.lon
        s, w, nn, e = self.bounds
        if not (s <= lat <= nn and w <= lon <= e):
            return
        self.coords[n.id] = _pack(lat, lon)
        tags = {t.k: t.v for t in n.tags}
        if not tags:
            return
        flags = 0
        hw = tags.get("highway")
        if hw == "traffic_signals":
            flags |= FLAG_SIGNAL
        if hw == "crossing":
            flags |= FLAG_CROSSING
        if hw in ("stop", "give_way"):
            flags |= FLAG_STOP
        if flags:
            self.node_tags[n.id] = flags
        name = tags.get("name")
        kind = _poi_kind(tags)
        if name and kind:
            en = tags.get("name:en")
            self.poi_nodes.append((n.id, name, en, _common_name(tags, name, en),
                                   _alias_names(tags, name, en), kind))


class _WayHandler(osmium.SimpleHandler):
    def __init__(self, coords, bounds):
        super().__init__()
        self.coords = coords
        self.bounds = bounds
        self.ways: list[tuple[int, dict, tuple[int, ...]]] = []
        self.poi_ways: list[tuple[tuple[int, ...], str, str, str | None, tuple[str, ...], str]] = []

    def way(self, w):
        tags = {t.k: t.v for t in w.tags}
        refs = tuple(n.ref for n in w.nodes)
        if not refs:
            return

        # Named features are collected regardless of routability: most malls,
        # temples and landmarks are areas rather than streets.
        kind = _poi_kind(tags)
        if kind:
            name = tags.get("name")
            en = tags.get("name:en")
            if name or en:
                self.poi_ways.append((refs, name, en, _common_name(tags, name, en),
                                      _alias_names(tags, name, en), kind))

        if not _is_routable_way(tags):
            return
        s, wst, nn, e = self.bounds
        inside = False
        for ref in refs:
            packed = self.coords.get(ref)
            if packed is None:
                continue
            lat, lon = _unpack(packed)
            if s <= lat <= nn and wst <= lon <= e:
                inside = True
                break
        if not inside:
            return
        self.ways.append((w.id, tags, refs))


class _RelationHandler(osmium.SimpleHandler):
    """Collect named POI relations (multipolygons and sites).

    Large landmarks such as the Grand Palace or Chulalongkorn University are
    mapped as relations, so they never appear as a single node or way.
    """

    def __init__(self):
        super().__init__()
        self.rels: list[tuple[str | None, str | None, str | None, tuple[str, ...], str, tuple[int, ...]]] = []
        self.member_ways: set[int] = set()

    def relation(self, r):
        tags = {t.k: t.v for t in r.tags}
        kind = _poi_kind(tags)
        if not kind:
            return
        name = tags.get("name")
        en = tags.get("name:en")
        if not (name or en):
            return
        refs = tuple(m.ref for m in r.members if m.type == "w")
        if not refs:
            return
        self.rels.append((name, en, _common_name(tags, name, en),
                          _alias_names(tags, name, en), kind, refs))
        self.member_ways.update(refs)


class _RestrictionHandler(osmium.SimpleHandler):
    """Collect turn restrictions (`type=restriction` relations).

    Each restriction forbids (or mandates) a movement through a via node: coming
    in along the `from` way and leaving along the `to` way. Only node-via
    restrictions are kept; the rarer way-via form is skipped because the via way
    can be entered at either end and honouring it needs per-segment logic.

    OSM restrictions are written for motor traffic. Thai law exempts bicycles
    from most posted turn bans, and `except=bicycle` marks the ones that
    explicitly do not apply, so those are dropped. A `restriction:bicycle` tag,
    when present, is authoritative and overrides the general one.
    """

    def __init__(self):
        super().__init__()
        self.restrictions: list[tuple[int, int, int, str]] = []
        self.way_via = 0
        self.exempt = 0
        self.other = 0

    def relation(self, r):
        tags = {t.k: t.v for t in r.tags}
        if tags.get("type") != "restriction":
            return
        kind = tags.get("restriction:bicycle") or tags.get("restriction")
        if not kind:
            self.other += 1
            return
        if "bicycle" in (tags.get("except") or "").split(";"):
            self.exempt += 1
            return

        frm = via = to = None
        via_is_way = False
        for m in r.members:
            if m.role == "from" and m.type == "w":
                frm = m.ref
            elif m.role == "to" and m.type == "w":
                to = m.ref
            elif m.role == "via":
                if m.type == "n":
                    via = m.ref
                else:
                    via_is_way = True
        if via_is_way:
            self.way_via += 1
            return
        if frm is None or via is None or to is None:
            self.other += 1
            return
        self.restrictions.append((frm, via, to, kind))


class _RelWayHandler(osmium.SimpleHandler):
    """Compute a centroid for each member way of a POI relation."""
    def __init__(self, coords, wanted: set[int]):
        super().__init__()
        self.coords = coords
        self.wanted = wanted
        self.centroid: dict[int, tuple[float, float]] = {}

    def way(self, w):
        if w.id not in self.wanted:
            return
        slat = slon = 0.0
        count = 0
        for n in w.nodes:
            packed = self.coords.get(n.ref)
            if packed is None:
                continue
            lat, lon = _unpack(packed)
            slat += lat
            slon += lon
            count += 1
        if count:
            self.centroid[w.id] = (slat / count, slon / count)


def build_extract(force: bool = False) -> dict:
    """Read the PBF into a compact dict, caching the result."""
    if EXTRACT_CACHE.exists() and not force:
        with EXTRACT_CACHE.open("rb") as fh:
            cached = pickle.load(fh)
        if cached.get("bbox") == BBOX and cached.get("schema") == EXTRACT_SCHEMA:
            return cached

    pbf = DATA_DIR / "bangkok.osm.pbf"
    if not pbf.exists():
        sys.exit(f"missing OSM extract: {pbf}")

    t0 = time.time()
    nodes = _NodeHandler(_bounds(BBOX, PAD_DEG))
    nodes.apply_file(str(pbf))
    ways = _WayHandler(nodes.coords, _bounds(BBOX))
    ways.apply_file(str(pbf))

    rels = _RelationHandler()
    rels.apply_file(str(pbf))
    poi_rels: list[tuple[str | None, str | None, str | None, tuple[str, ...], str, float, float]] = []
    if rels.member_ways:
        rel_ways = _RelWayHandler(nodes.coords, rels.member_ways)
        rel_ways.apply_file(str(pbf))
        for name, en, common, alts, kind, refs in rels.rels:
            pts = [rel_ways.centroid[r] for r in refs if r in rel_ways.centroid]
            if not pts:
                continue
            poi_rels.append((
                name, en, common, alts, kind,
                sum(p[0] for p in pts) / len(pts),
                sum(p[1] for p in pts) / len(pts),
            ))

    # Turn restrictions only need the relation blocks, so this pass is cheap.
    restr = _RestrictionHandler()
    restr.apply_file(str(pbf))

    print(f"  extract: {len(ways.ways):,} routable ways, {len(nodes.coords):,} nodes, "
          f"{len(poi_rels):,} relation POIs in {time.time() - t0:.1f}s")
    print(f"  turn restrictions: {len(restr.restrictions):,} usable "
          f"({restr.exempt:,} exempt bicycles, {restr.way_via:,} way-via skipped)")

    extract = {
        "schema": EXTRACT_SCHEMA,
        "bbox": BBOX,
        "coords": nodes.coords,
        "node_tags": nodes.node_tags,
        "ways": ways.ways,
        "poi_nodes": nodes.poi_nodes,
        "poi_ways": ways.poi_ways,
        "poi_rels": poi_rels,
        "restrictions": restr.restrictions,
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with EXTRACT_CACHE.open("wb") as fh:
        pickle.dump(extract, fh, protocol=pickle.HIGHEST_PROTOCOL)
    return extract


# --------------------------------------------------------------------------
# stage 2: graph
# --------------------------------------------------------------------------
def _oneway_directions(tags: dict) -> tuple[bool, bool]:
    """Return (forward_allowed, backward_allowed) for a way."""
    oneway = (tags.get("oneway") or "").strip().lower()
    bicycle_oneway = (tags.get("oneway:bicycle") or "").strip().lower()

    forward = backward = True
    if oneway in ("yes", "1", "true"):
        forward, backward = True, False
    elif oneway in ("-1", "reverse"):
        forward, backward = False, True

    # Bicycles are frequently exempt from one-way restrictions (contraflow).
    if bicycle_oneway in ("no", "false", "0"):
        forward = backward = True
    elif bicycle_oneway in ("yes", "1", "true"):
        forward, backward = True, False
    elif bicycle_oneway in ("-1", "reverse"):
        forward, backward = False, True

    if tags.get("bicycle:backward") in ("no", "private"):
        backward = False
    if tags.get("bicycle:forward") in ("no", "private"):
        forward = False
    return forward, backward


def _edge_metrics(tags: dict, length_m: float) -> tuple[float, float, float, float, str]:
    """Return (lts, risk, time_s, penalty_s, reason) for a way segment."""
    if tags.get("route") == "ferry":
        speed = DEFAULT_FERRY_SPEED_KMH
        duration = tags.get("duration")
        if duration:
            try:
                secs = 0.0
                for part in str(duration).split(":"):
                    secs = secs * 60 + float(part)
                if secs > 0 and length_m > 0:
                    speed = max(2.0, min(40.0, length_m / secs * 3.6))
            except ValueError:
                pass
        return 1.0, 0.0, length_m / (speed / 3.6), 0.0, "river ferry"

    lts, reason = level_of_traffic_stress(tags)
    if lts is None:
        lts, reason = 4.0, "unknown"
    speed = bicycle_speed_kmh(tags, tags.get("highway"), lts)
    penalty = access_penalty_s(tags)
    if penalty:
        access = (tags.get("access") or "ford").strip().lower()
        reason += f", {access} access"
    return lts, risk_from_lts(lts), length_m / (speed / 3.6), penalty, reason


def build_graph(extract: dict, force: bool = False) -> dict:
    if GRAPH_CACHE.exists() and not force:
        with GRAPH_CACHE.open("rb") as fh:
            cached = pickle.load(fh)
        # The graph is derived from the extract, so a re-parsed extract must
        # invalidate it too -- otherwise widening POI_RULES would leave the old
        # gazetteer in place behind a stale cache hit.
        if (cached.get("bbox") == BBOX
                and cached.get("schema") == GRAPH_SCHEMA
                and cached.get("extract_schema") == extract.get("schema")):
            return cached

    coords = extract["coords"]
    node_tags = extract["node_tags"]
    ways = extract["ways"]

    # A node becomes a routing vertex if it is shared between ways, is a way
    # endpoint, or carries a junction tag we penalise.
    use_count: Counter[int] = Counter()
    for _wid, _tags, refs in ways:
        for ref in refs:
            use_count[ref] += 1

    node_index: dict[int, int] = {}
    node_lat: array = array("d")
    node_lon: array = array("d")

    def node_idx(ref: int, lat: float, lon: float) -> int:
        idx = node_index.get(ref)
        if idx is None:
            idx = len(node_lat)
            node_index[ref] = idx
            node_lat.append(lat)
            node_lon.append(lon)
        return idx

    edge_u = array("i")
    edge_v = array("i")
    edge_len = array("d")
    edge_time = array("d")
    edge_lts = array("d")
    edge_risk = array("d")
    edge_pen = array("d")
    edge_way = array("q")
    edge_rev = array("i")
    edge_bear_start = array("d")
    edge_bear_end = array("d")
    geom_coords = array("d")
    geom_off = array("i", [0])
    edge_name: list[str] = []
    edge_name_alt: list[str] = []
    edge_reason: list[str] = []
    edge_hw: list[str] = []

    skipped_missing = 0

    def add_edge(u, v, geom, length_m, tags, way_id) -> int:
        lts, risk, time_s, penalty, reason = _edge_metrics(tags, length_m)
        idx = len(edge_u)
        edge_u.append(u)
        edge_v.append(v)
        edge_len.append(length_m)
        edge_time.append(time_s)
        edge_lts.append(lts)
        edge_risk.append(risk)
        edge_pen.append(penalty)
        edge_way.append(way_id)
        edge_rev.append(-1)
        if len(geom) > 1:
            edge_bear_start.append(bearing_deg(geom[0][0], geom[0][1], geom[1][0], geom[1][1]))
            edge_bear_end.append(bearing_deg(geom[-2][0], geom[-2][1], geom[-1][0], geom[-1][1]))
        else:
            edge_bear_start.append(0.0)
            edge_bear_end.append(0.0)
        for lat, lon in geom:
            geom_coords.append(lat)
            geom_coords.append(lon)
        geom_off.append(len(geom_coords) // 2)
        en = tags.get("name:en")
        native = tags.get("name")
        edge_name.append(en or native or "")
        edge_name_alt.append(native if (en and native and native != en) else "")
        edge_reason.append(reason)
        edge_hw.append(tags.get("highway") or "ferry")
        return idx

    for _wid, tags, refs in ways:
        fwd, bwd = _oneway_directions(tags)
        if not (fwd or bwd):
            continue

        last_i = len(refs) - 1
        run_start_ref: int | None = None
        run: list[tuple[float, float]] = []
        run_len = 0.0
        prev: tuple[float, float] | None = None

        for i, ref in enumerate(refs):
            packed = coords.get(ref)
            if packed is None:
                skipped_missing += 1
                run_start_ref, run, run_len, prev = None, [], 0.0, None
                continue

            lat, lon = _unpack(packed)

            if run_start_ref is None:
                run_start_ref = ref
                run = [(lat, lon)]
                run_len = 0.0
                prev = (lat, lon)
                continue

            run_len += haversine_m(prev[0], prev[1], lat, lon)
            run.append((lat, lon))
            prev = (lat, lon)

            if not (i == last_i or use_count[ref] > 1 or ref in node_tags):
                continue

            if len(run) >= 2 and run_len > 0.5:
                u = node_idx(run_start_ref, run[0][0], run[0][1])
                v = node_idx(ref, lat, lon)
                a = b = -1
                if fwd:
                    a = add_edge(u, v, run, run_len, tags, _wid)
                if bwd:
                    b = add_edge(v, u, list(reversed(run)), run_len, tags, _wid)
                if fwd and bwd:
                    edge_rev[a] = b
                    edge_rev[b] = a

            run_start_ref = ref
            run = [(lat, lon)]
            run_len = 0.0
            prev = (lat, lon)

    n_nodes = len(node_lat)
    n_edges = len(edge_u)

    adj: list[list[int]] = [[] for _ in range(n_nodes)]
    for e in range(n_edges):
        adj[edge_u[e]].append(e)

    # --- connectivity ------------------------------------------------------
    parent = list(range(n_nodes))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for e in range(n_edges):
        ru, rv = find(edge_u[e]), find(edge_v[e])
        if ru != rv:
            parent[ru] = rv

    roots = [find(i) for i in range(n_nodes)]
    comp_sizes: Counter[int] = Counter(roots)
    comp = array("i", roots)
    main_comp = comp_sizes.most_common(1)[0][0] if comp_sizes else -1

    signal_nodes = {node_index[r] for r, f in node_tags.items() if f & FLAG_SIGNAL and r in node_index}
    crossing_nodes = {node_index[r] for r, f in node_tags.items() if f & FLAG_CROSSING and r in node_index}
    stop_nodes = {node_index[r] for r, f in node_tags.items() if f & FLAG_STOP and r in node_index}

    # --- turn restrictions -------------------------------------------------
    # A restriction names ways, but the graph stores *edges* (a way split at
    # junctions), so each one is expanded into the concrete edge pairs it
    # forbids: the `from` way's edges arriving at the via node, against the `to`
    # way's edges leaving it.
    banned: dict[int, set[int]] = {}
    applied = 0
    unenforceable = 0

    def ban(i: int, o: int) -> None:
        banned.setdefault(i, set()).add(o)

    if extract.get("restrictions"):
        out_by_node_way: dict[tuple[int, int], list[int]] = {}
        in_by_node_way: dict[tuple[int, int], list[int]] = {}
        for e in range(n_edges):
            out_by_node_way.setdefault((edge_u[e], edge_way[e]), []).append(e)
            in_by_node_way.setdefault((edge_v[e], edge_way[e]), []).append(e)

        for frm_way, via_ref, to_way, kind in extract["restrictions"]:
            via = node_index.get(via_ref)
            if via is None:
                unenforceable += 1
                continue
            ins = in_by_node_way.get((via, frm_way))
            outs = out_by_node_way.get((via, to_way))
            if not ins or not outs:
                unenforceable += 1
                continue
            if kind.startswith("only_"):
                allowed = set(outs)
                for i in ins:
                    for o in adj[via]:
                        if o not in allowed:
                            ban(i, o)
            else:
                for i in ins:
                    for o in outs:
                        ban(i, o)
            applied += 1

    banned_turns = {i: sorted(outs) for i, outs in banned.items()}

    # --- POIs --------------------------------------------------------------
    pois: list[dict] = []
    seen: set[tuple[str, int, int]] = set()

    def add_poi(name: str | None, en: str | None, common: str | None,
                alts: tuple[str, ...], kind: str, lat: float, lon: float) -> None:
        canonical = common or en or name or ""
        # Exit numbers and similar refs ("1", "3A") are not useful endpoints.
        if not any(ch.isalpha() for ch in canonical):
            return
        official = en or name
        alt = official if (official and official != canonical) else None
        key = (canonical, round(lat * 1e4), round(lon * 1e4))
        if key in seen:
            return
        seen.add(key)
        pois.append({"name": canonical, "alt": alt, "alts": list(alts),
                     "kind": kind, "lat": lat, "lon": lon})

    for ref, name, en, common, alts, kind in extract["poi_nodes"]:
        packed = coords.get(ref)
        if packed is not None:
            lat, lon = _unpack(packed)
            add_poi(name, en, common, alts, kind, lat, lon)
    for refs, name, en, common, alts, kind in extract["poi_ways"]:
        pts = [_unpack(coords[r]) for r in refs if r in coords]
        if pts:
            add_poi(name, en, common, alts, kind,
                    sum(p[0] for p in pts) / len(pts),
                    sum(p[1] for p in pts) / len(pts))
    for name, en, common, alts, kind, lat, lon in extract.get("poi_rels", ()):
        add_poi(name, en, common, alts, kind, lat, lon)
    pois.sort(key=lambda p: p["name"])

    lts_hist: Counter[int] = Counter()
    total_km = 0.0
    for e in range(n_edges):
        total_km += edge_len[e] / 1000.0
        lts_hist[int(round(edge_lts[e]))] += 1

    graph = {
        "schema": GRAPH_SCHEMA,
        "extract_schema": extract.get("schema"),
        "bbox": BBOX,
        "node_lat": node_lat,
        "node_lon": node_lon,
        "edge_u": edge_u,
        "edge_v": edge_v,
        "edge_len": edge_len,
        "edge_time": edge_time,
        "edge_lts": edge_lts,
        "edge_risk": edge_risk,
        "edge_pen": edge_pen,
        "edge_way": edge_way,
        "edge_rev": edge_rev,
        "edge_bear_start": edge_bear_start,
        "edge_bear_end": edge_bear_end,
        "geom_coords": geom_coords,
        "geom_off": geom_off,
        "edge_name": edge_name,
        "edge_name_alt": edge_name_alt,
        "edge_reason": edge_reason,
        "edge_hw": edge_hw,
        "adj": adj,
        "comp": comp,
        "main_comp": main_comp,
        "signal_nodes": signal_nodes,
        "crossing_nodes": crossing_nodes,
        "stop_nodes": stop_nodes,
        "banned_turns": banned_turns,
        "pois": pois,
        "stats": {
            "nodes": n_nodes,
            "edges": n_edges,
            "components": len(comp_sizes),
            "main_component_nodes": comp_sizes.most_common(1)[0][1] if comp_sizes else 0,
            "total_km": round(total_km, 1),
            "lts_histogram": dict(sorted(lts_hist.items())),
            "skipped_missing_nodes": skipped_missing,
            "pois": len(pois),
            "restrictions_applied": applied,
            "restrictions_unenforceable": unenforceable,
            "banned_turns": len(banned_turns),
            "penalised_edges": sum(1 for p in edge_pen if p > 0),
        },
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with GRAPH_CACHE.open("wb") as fh:
        pickle.dump(graph, fh, protocol=pickle.HIGHEST_PROTOCOL)
    return graph


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the bicycle routing graph")
    parser.add_argument("--force", action="store_true", help="ignore caches and rebuild")
    args = parser.parse_args()

    print("building extract ...")
    extract = build_extract(force=args.force)
    print("building graph ...")
    graph = build_graph(extract, force=args.force)

    st = graph["stats"]
    print("\n--- graph summary ---")
    print(f"  routing nodes : {st['nodes']:,}")
    print(f"  directed edges: {st['edges']:,}")
    print(f"  road length   : {st['total_km']:,} km")
    print(f"  components    : {st['components']:,} "
          f"(largest {st['main_component_nodes']:,} nodes)")
    print(f"  named places  : {st['pois']:,}")
    print(f"  LTS histogram : {st['lts_histogram']}")
    print(f"  turn bans     : {st['restrictions_applied']:,} restrictions applied "
          f"({st['restrictions_unenforceable']:,} not enforceable), "
          f"{st['banned_turns']:,} entry edges affected")
    print(f"  penalised edges: {st['penalised_edges']:,} (private access / ford)")


if __name__ == "__main__":
    main()
