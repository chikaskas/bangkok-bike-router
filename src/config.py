"""Central configuration for the RideRouter MVP."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
WEB_DIR = ROOT / "web"
OUT_DIR = ROOT / "out"

THAILAND_PBF = DATA_DIR / "thailand-latest.osm.pbf"
EXTRACT_CACHE = DATA_DIR / "extract.pkl"
GRAPH_CACHE = DATA_DIR / "graph.pkl"

# Central Bangkok: (south, west, north, east).
# Covers Rattanakosin/Thonburi in the west, Chatuchak in the north,
# Sukhumvit/Bang Na in the east and the port area in the south.
BBOX = (13.6400, 100.4000, 13.8600, 100.6600)

# Highways a bicycle may not use at all.
IMPASSABLE_HIGHWAY = {
    "motorway",
    "motorway_link",
    "construction",
    "proposed",
    "raceway",
    "bus_guideway",
}

# Highway classes we will never route on, regardless of other tags.
NON_ROUTABLE_HIGHWAY = IMPASSABLE_HIGHWAY | {"steps", "corridor", "elevator", "platform"}

# OSM highway classes where bicycles are forbidden unless explicitly allowed.
BICYCLE_FORBIDDEN_BY_DEFAULT = {"footway", "pedestrian", "steps", "corridor"}

# Default motor-vehicle speed (km/h) used when `maxspeed` is missing.
# Bangkok tags maxspeed on only a minority of streets, so these defaults do
# most of the work. Values follow Thai urban design speeds.
DEFAULT_MAXSPEED = {
    "trunk": 80,
    "trunk_link": 50,
    "primary": 60,
    "primary_link": 45,
    "secondary": 50,
    "secondary_link": 40,
    "tertiary": 40,
    "tertiary_link": 35,
    "unclassified": 40,
    "residential": 30,
    "living_street": 15,
    "service": 15,
    "road": 30,
    "track": 15,
    "cycleway": 15,
    "path": 15,
    "footway": 10,
    "pedestrian": 10,
}

# Default bicycle speed (km/h) by highway class.
DEFAULT_BIKE_SPEED = {
    "cycleway": 18,
    "path": 15,
    "track": 12,
    "footway": 10,
    "pedestrian": 10,
    "living_street": 16,
    "residential": 17,
    "service": 15,
    "road": 15,
    "unclassified": 17,
    "tertiary": 17,
    "tertiary_link": 16,
    "secondary": 17,
    "secondary_link": 16,
    "primary": 17,
    "primary_link": 16,
    "trunk": 16,
    "trunk_link": 15,
}

# --- traffic-volume proxy ---------------------------------------------------
# Lane count is the best available stand-in for how much traffic a rider meets,
# but `lanes` is tagged on only a small share of Bangkok ways, so `width` is
# used as a fallback and converted at this many metres per lane.
WIDTH_PER_LANE_M = 3.2

# A way with no bicycle facility that is this narrow, this slow and carries no
# route number is scored as a quiet local street rather than as a generic road
# of its class. Road class alone cannot tell a leafy soi from a through route,
# and without this the two are lumped together at LTS 3.
QUIET_CLASSES = {"tertiary", "tertiary_link", "unclassified", "residential"}
QUIET_MAX_LANES = 2.0
QUIET_MAX_SPEED_KMH = 40.0
QUIET_LTS = 2.5

# Bicycle speed multiplier by `surface` tag.
SURFACE_SPEED_FACTOR = {
    "paved": 1.0,
    "asphalt": 1.0,
    "concrete": 0.98,
    "concrete:plates": 0.92,
    "concrete:lanes": 0.92,
    "paving_stones": 0.9,
    "sett": 0.75,
    "cobblestone": 0.7,
    "unhewn_cobblestone": 0.6,
    "compacted": 0.88,
    "fine_gravel": 0.82,
    "gravel": 0.75,
    "pebblestone": 0.7,
    "dirt": 0.6,
    "earth": 0.6,
    "ground": 0.6,
    "mud": 0.45,
    "sand": 0.4,
    "grass": 0.6,
    "wood": 0.8,
    "metal": 0.95,
}

# Surfaces considered rough for the LTS penalty and speed reduction.
UNPAVED_SURFACES = {
    "unpaved",
    "compacted",
    "fine_gravel",
    "gravel",
    "pebblestone",
    "dirt",
    "earth",
    "ground",
    "mud",
    "sand",
    "grass",
    "cobblestone",
    "unhewn_cobblestone",
    "sett",
    "wood",
}

# --- Safety preference slider -------------------------------------------------
# Generalized cost is:  cost = time_seconds * (1 + lambda * risk(edge))
# lambda = 0 reproduces the pure fastest route; larger values buy safety with time.
LAMBDA_SWEEP = (0.0, 0.3, 0.8, 1.6, 3.0, 6.0, 12.0)
# The slider is continuous, so it maps its whole travel onto this interval. The
# sweep above is only used for the "compare alternatives" view.
MIN_LAMBDA = 0.0
MAX_LAMBDA = 12.0

# --- Access restrictions -----------------------------------------------------
# `access` values that mean "not for the general public". Bangkok tags these on
# ~22k ways (gated moo baan sois, private driveways, car-park aisles). Banning
# them outright risks cutting the network in half, so instead they are charged a
# large fixed penalty: the router avoids them whenever a legal alternative
# exists, but a trip that genuinely has no other way through still returns a
# route instead of failing.
RESTRICTIVE_ACCESS = {
    "private",
    "no",
    "customers",
    "permit",
    "agricultural",
    "forestry",
    "delivery",
    "military",
}
# Bicycle tags that override a restrictive `access` on the same way.
ACCESS_OVERRIDE = {"yes", "designated", "permissive", "destination"}
ACCESS_PENALTY_S = 600.0
# A crossing with no bridge or ford is impassable for a road bike.
FORD_PENALTY_S = 900.0

# `smoothness` affects both speed and stress. Values are speed multipliers.
SMOOTHNESS_SPEED_FACTOR = {
    "excellent": 1.05,
    "good": 1.0,
    "intermediate": 0.85,
    "bad": 0.65,
    "very_bad": 0.5,
    "horrible": 0.4,
    "very_horrible": 0.3,
    "impassable": 0.2,
}
# Smoothness values rough enough to raise the stress level.
ROUGH_SMOOTHNESS = {"bad", "very_bad", "horrible", "very_horrible", "impassable"}

# --- Junction penalties (seconds) --------------------------------------------
# Time only: these are charged to the clock, not to rider stress.
TURN_PENALTY_S = 4.0
SHARP_TURN_PENALTY_S = 8.0
RIGHT_TURN_EXTRA_S = 6.0
TRAFFIC_SIGNAL_PENALTY_S = 15.0
STOP_PENALTY_S = 8.0
CROSSING_PENALTY_S = 5.0

# --- Junction exposure (risk) ------------------------------------------------
# Junctions, not links, are where cyclists are actually hit, so they carry a
# safety charge as well as a time charge. Each junction is modelled as extra
# *exposure seconds* spent in conflict with traffic, charged at the risk of the
# busiest road meeting there (an unsignalised crossing of a 6-lane arterial is
# not the same manoeuvre as crossing a soi). Exposure is charged to stress only,
# never to the clock, so reported travel times stay physical.
#
# Consequence: the router prefers to cross an arterial at a signal rather than
# mid-block, and will detour to avoid turning right across oncoming traffic.
JUNCTION_SIGNAL_EXPOSURE_S = 6.0
JUNCTION_CROSSING_EXPOSURE_S = 9.0
JUNCTION_STOP_EXPOSURE_S = 5.0
# A right turn crosses oncoming traffic (Thailand drives on the left).
JUNCTION_RIGHT_TURN_EXPOSURE_S = 7.0
# Even where both roads are quiet, an uncontrolled junction is a conflict point.
JUNCTION_RISK_FLOOR = 0.15
# One junction manoeuvre is charged to reported comfort as the equivalent of
# this many metres of road at the junction's own stress level. Junctions are not
# distance, but leaving them out entirely would let a route that crosses six
# unsignalised arterials still report the same comfort as one that crosses none.
JUNCTION_EQUIV_LEN_M = 50.0

# Angles in degrees. Below STRAIGHT_ANGLE the movement counts as going straight.
STRAIGHT_ANGLE = 30.0
SHARP_ANGLE = 100.0

# Thailand drives on the left, so a "right" turn crosses oncoming traffic.
DRIVE_ON_LEFT = True

# --- Ferry ------------------------------------------------------------------
DEFAULT_FERRY_SPEED_KMH = 10.0

# Search radius for snapping a query point onto the graph (metres).
SNAP_RADIUS_M = 400.0

# --- Server -----------------------------------------------------------------
SERVER_HOST = "127.0.0.1"
SERVER_PORT = 8000
