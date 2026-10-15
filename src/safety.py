"""Bicycle safety scoring based on Level of Traffic Stress (LTS).

Adapted from Mekuria, Furth & Nixon (2012), "Low-Stress Bicycling and Network
Connectivity", Transportation Research Record 2247, pp. 33-41.

LTS is a four-level scale describing who would be willing to ride a link:

    1  separated from traffic, or very calm - most people, including children
    2  comfortable for most adults
    3  comfortable only for confident, experienced adults
    4  only the "strong and fearless"

The paper's thresholds are in mph; they are converted to km/h here and the
default speeds are retuned for Bangkok, where `maxspeed` is tagged on only a
minority of streets and the hierarchy runs from 8-lane arterials down to quiet
residential sois.
"""

from __future__ import annotations

import math
import re

from config import (
    ACCESS_OVERRIDE,
    ACCESS_PENALTY_S,
    BICYCLE_FORBIDDEN_BY_DEFAULT,
    DEFAULT_BIKE_SPEED,
    DEFAULT_MAXSPEED,
    FORD_PENALTY_S,
    NON_ROUTABLE_HIGHWAY,
    RESTRICTIVE_ACCESS,
    ROUGH_SMOOTHNESS,
    SMOOTHNESS_SPEED_FACTOR,
    SURFACE_SPEED_FACTOR,
    UNPAVED_SURFACES,
    QUIET_CLASSES,
    QUIET_LTS,
    QUIET_MAX_LANES,
    QUIET_MAX_SPEED_KMH,
    WIDTH_PER_LANE_M,
)

LTS_MIN = 1.0
LTS_MAX = 4.0

_NUM_RE = re.compile(r"(\d+(?:\.\d+)?)")


def parse_maxspeed(value: str | None) -> float | None:
    """Parse an OSM `maxspeed` value into km/h, or None when unusable."""
    if not value:
        return None
    v = value.strip().lower()
    if v in ("none", "signals", "variable", "unknown", "default"):
        return None
    if v == "walk":
        return 7.0
    mph = "mph" in v
    knots = "knot" in v
    match = _NUM_RE.search(v)
    if not match:
        return None
    speed = float(match.group(1))
    if mph:
        speed *= 1.60934
    elif knots:
        speed *= 1.852
    return speed


def effective_motor_speed(tags: dict, highway: str | None) -> float:
    """Motor-vehicle design/operating speed in km/h for a way."""
    speed = parse_maxspeed(tags.get("maxspeed"))
    if speed is None:
        speed = DEFAULT_MAXSPEED.get(highway or "", 30.0)
    return speed


def _facility(tags: dict) -> tuple[bool, bool]:
    """Return (has_bike_lane, has_separated_facility) from cycleway tagging."""
    values: list[str] = []
    for key in ("cycleway", "cycleway:left", "cycleway:right", "cycleway:both"):
        val = tags.get(key)
        if val:
            values.extend(part.strip() for part in val.split(";"))

    lane_tags = {"lane", "opposite_lane", "shared_lane", "share_busway", "opposite_share_busway"}
    track_tags = {"track", "opposite_track", "sidepath", "crossing", "separate"}

    has_lane = any(v in lane_tags for v in values)
    has_track = any(v in track_tags for v in values)

    if tags.get("segregated") == "yes":
        has_track = True
    if tags.get("bicycle") == "designated" and tags.get("highway") == "cycleway":
        has_track = True
    return has_lane, has_track


def level_of_traffic_stress(tags: dict) -> tuple[float | None, str]:
    """Score a way's traffic stress.

    Returns ``(lts, reason)`` where ``lts`` is a float in [1, 4], or
    ``(None, reason)`` when the way is not usable by bicycle at all.
    """
    highway = tags.get("highway")
    bicycle = tags.get("bicycle")

    if bicycle in ("no", "private", "dismount"):
        return None, "bicycles not permitted"

    if highway in NON_ROUTABLE_HIGHWAY:
        return None, f"not routable ({highway})"

    if highway is None:
        return None, "no highway tag"

    # Explicit bicycle permission overrides the default-deny classes.
    if highway in BICYCLE_FORBIDDEN_BY_DEFAULT and bicycle not in (
        "yes",
        "designated",
        "permissive",
    ):
        return None, f"bicycles not permitted on {highway}"

    has_lane, has_track = _facility(tags)
    speed = effective_motor_speed(tags, highway)

    if highway == "cycleway":
        lts, reason = 1.0, "dedicated cycleway"
    elif has_track:
        lts, reason = 1.0, "separated cycle track"
    elif highway in ("path", "footway", "pedestrian"):
        lts, reason = 1.0, "traffic-free path"
    elif highway == "track":
        lts, reason = 2.5, "farm/utility track"
    elif has_lane:
        if speed <= 40:
            lts, reason = 2.0, f"bike lane on {speed:.0f} km/h road"
        elif speed <= 50:
            lts, reason = 3.0, f"bike lane on {speed:.0f} km/h road"
        else:
            lts, reason = 3.5, f"bike lane on fast {speed:.0f} km/h road"
    elif highway == "living_street":
        lts, reason = 1.5, "living street"
    elif highway == "service":
        # `service` covers everything from a car-park aisle to a through alley.
        # The subtype is tagged on most of them in Bangkok, and it is the single
        # best available proxy for how much traffic a rider meets.
        subtype = (tags.get("service") or "").strip().lower()
        if subtype in ("driveway", "parking_aisle", "parking", "emergency_access"):
            lts, reason = 1.5, f"service {subtype.replace('_', ' ')}"
        elif subtype in ("drive-through", "driving_learning"):
            lts, reason = 2.5, f"service {subtype.replace('_', ' ')}"
        else:
            lts, reason = 2.0, "service alley"
    elif highway == "residential":
        if speed <= 30:
            lts, reason = 2.0, "quiet residential soi"
        else:
            lts, reason = 3.0, f"residential street at {speed:.0f} km/h"
    elif highway == "unclassified":
        if speed <= 30:
            lts, reason = 2.5, "narrow soi"
        elif speed <= 40:
            lts, reason = 3.0, f"soi at {speed:.0f} km/h"
        else:
            lts, reason = 3.5, f"soi at {speed:.0f} km/h"
    elif highway in ("tertiary", "tertiary_link"):
        if speed <= 40:
            lts, reason = 3.0, f"mixed traffic on {speed:.0f} km/h road"
        else:
            lts, reason = 3.5, f"mixed traffic on {speed:.0f} km/h road"
    elif highway in ("secondary", "secondary_link"):
        if speed <= 50:
            lts, reason = 3.5, f"mixed traffic on {speed:.0f} km/h arterial"
        else:
            lts, reason = 4.0, f"mixed traffic on fast {speed:.0f} km/h arterial"
    elif highway in ("primary", "primary_link", "trunk", "trunk_link"):
        lts, reason = 4.0, f"major arterial, {speed:.0f} km/h"
    else:
        lts, reason = 3.5, f"unclassified road type ({highway})"

    # --- traffic volume ----------------------------------------------------
    # Road class alone cannot separate a leafy local street from a through
    # route, so every low-class way without a bicycle facility is otherwise
    # charged LTS 3 for the sole reason that it is a road. Where the tags
    # independently agree the way is narrow, slow and unnumbered, it is scored
    # as the quiet street it is. Mekuria et al. place mixed traffic at or below
    # 40 km/h with at most three lanes at LTS 2; this keeps a Bangkok margin
    # above that rather than following the paper down to LTS 2.
    lanes = _lane_count(tags)
    if (
        not (has_lane or has_track)
        and highway in QUIET_CLASSES
        and lts >= 2.75
        and lanes is not None
        and lanes <= QUIET_MAX_LANES
        and speed <= QUIET_MAX_SPEED_KMH
        and not tags.get("ref")
    ):
        lts = QUIET_LTS
        reason = f"quiet {speed:.0f} km/h road, light traffic"

    # --- modifiers ---------------------------------------------------------
    if lanes is not None:
        if has_track:
            # Physically separated, so the number of motor lanes does not change
            # what the rider experiences.
            pass
        elif has_lane:
            # A painted lane only keeps stress low while the road stays narrow.
            # The LTS 2 band assumes at most two lanes; beyond that a bike lane
            # is still a bike lane beside a lot of traffic.
            if lanes >= 4:
                lts = max(lts, 3.5)
                reason += f", {lanes:g} lanes"
            elif lanes >= 3:
                lts = max(lts, 2.5)
                reason += f", {lanes:g} lanes"
        elif lanes >= 4:
            lts = max(lts, 4.0)
            reason += ", 4+ lanes"
        elif lanes >= 3:
            lts = max(lts, 3.5)
        elif lanes >= 2 and lts <= 2.25:
            # Two lanes on an otherwise comfortable soi means through traffic.
            lts = min(LTS_MAX, lts + 0.25)
            reason += f", {lanes:g} lanes"

    if tags.get("junction") == "roundabout" and not (has_lane or has_track):
        lts = min(LTS_MAX, lts + 0.5)
        reason += ", roundabout"

    surface = tags.get("surface")
    if surface and surface in UNPAVED_SURFACES:
        lts = min(LTS_MAX, lts + 0.5)
        reason += f", {surface} surface"

    smoothness = tags.get("smoothness")
    if smoothness in ROUGH_SMOOTHNESS:
        lts = min(LTS_MAX, lts + 0.5)
        reason += f", {smoothness} surface"

    if tags.get("lit") == "no":
        lts = min(LTS_MAX, lts + 0.25)
        reason += ", unlit"

    # Riding in the traffic lane with no footway beside it leaves nowhere to bail
    # out. Only meaningful while the road is otherwise calm.
    if tags.get("sidewalk") == "no" and not (has_lane or has_track) and lts <= 2.5:
        lts = min(LTS_MAX, lts + 0.25)
        reason += ", no sidewalk"

    return max(LTS_MIN, min(LTS_MAX, lts)), reason


def access_penalty_s(tags: dict) -> float:
    """Fixed cost for ways the public is not meant to ride on.

    Returned in seconds and added to the link cost so the search avoids private
    driveways and gated sois when a legal route exists, without making a trip
    that has no alternative fail outright.
    """
    penalty = 0.0
    access = (tags.get("access") or "").strip().lower()
    bicycle = (tags.get("bicycle") or "").strip().lower()
    if access in RESTRICTIVE_ACCESS and bicycle not in ACCESS_OVERRIDE:
        penalty += ACCESS_PENALTY_S
    if tags.get("ford") in ("yes", "true", "1"):
        penalty += FORD_PENALTY_S
    return penalty


def _parse_lanes(value: str | None) -> float | None:
    if not value:
        return None
    total = 0.0
    found = False
    for part in str(value).split(";"):
        part = part.strip().lower()
        if not part or part in ("none", "0"):
            continue
        match = _NUM_RE.search(part)
        if match:
            total += float(match.group(1))
            found = True
    return total if found else None


def _parse_length_m(value: str | None) -> float | None:
    """Parse an OSM length tag such as `width` into metres."""
    if not value:
        return None
    v = str(value).strip().lower()
    if v in ("none", "default", "unknown", "variable", "narrow"):
        return None
    match = _NUM_RE.search(v)
    if not match:
        return None
    metres = float(match.group(1))
    if "ft" in v or "'" in v:
        metres *= 0.3048
    return metres if metres > 0 else None


def _lane_count(tags: dict) -> float | None:
    """Best available estimate of the motor-vehicle lanes on a way.

    `lanes` is the direct answer but is tagged on only a small share of Bangkok
    ways, so directional counts are summed and `width` is used as a last
    resort. A width-derived figure is an estimate, not a survey, which is why
    it is only ever used to *lower* the stress of a demonstrably narrow way.
    """
    total = _parse_lanes(tags.get("lanes"))
    if total is None:
        known = [
            n
            for n in (_parse_lanes(tags.get(k)) for k in ("lanes:forward", "lanes:backward"))
            if n is not None
        ]
        if known:
            total = sum(known)
    if total is not None:
        return total
    width = _parse_length_m(tags.get("width"))
    if width is not None:
        return width / WIDTH_PER_LANE_M
    return None


def bicycle_speed_kmh(tags: dict, highway: str | None, lts: float) -> float:
    """Free-flow bicycle speed in km/h for a way."""
    speed = DEFAULT_BIKE_SPEED.get(highway or "", 15.0)
    surface = tags.get("surface")
    if surface:
        speed *= SURFACE_SPEED_FACTOR.get(surface, 0.85)
    smoothness = tags.get("smoothness")
    if smoothness:
        speed *= SMOOTHNESS_SPEED_FACTOR.get(smoothness, 0.9)
    # Higher-stress roads force more braking, weaving and waiting.
    if lts >= 3.5:
        speed *= 0.9
    return max(5.0, speed)


def risk_from_lts(lts: float) -> float:
    """Map LTS onto a dimensionless risk multiplier used in generalized cost.

    Quadratic in the stress above LTS 1, reaching 2.5 at LTS 4. Combined with
    ``cost = time * (1 + lambda * risk)`` an LTS 4 link is charged 3.5x its
    travel time at lambda = 1 and 31x at lambda = 12.
    """
    if lts <= LTS_MIN:
        return 0.0
    frac = (lts - LTS_MIN) / (LTS_MAX - LTS_MIN)
    return round(2.5 * frac * frac, 4)


def lts_from_risk(risk: float) -> float:
    """Inverse of :func:`risk_from_lts`, clamped to the LTS range."""
    if risk <= 0.0:
        return LTS_MIN
    frac = math.sqrt(risk / 2.5)
    return max(LTS_MIN, min(LTS_MAX, LTS_MIN + frac * (LTS_MAX - LTS_MIN)))


# Short labels for the UI legend.
LTS_LABELS = {
    1: "LTS 1 - traffic-free",
    2: "LTS 2 - comfortable",
    3: "LTS 3 - confident riders",
    4: "LTS 4 - high stress",
}


def lts_bucket(lts: float) -> int:
    """Bucket a continuous LTS score into 1..4 for display."""
    return max(1, min(4, int(round(lts))))
