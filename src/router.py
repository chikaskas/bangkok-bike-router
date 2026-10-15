"""Multi-objective bicycle routing over the pre-built Bangkok graph.

Generalized cost of a link is ``time_s * (1 + lambda * risk)``.  Sweeping
lambda from 0 upwards traces the Pareto front between "fastest" and "calmest".
Junction delays (turns, signals, stop lines, crossings) are folded into the
link that *enters* the junction so that a left-turn-only route is genuinely
cheaper than one that keeps crossing oncoming traffic.
"""
from __future__ import annotations

import heapq
import math
import pickle
import sys

from config import (
    CROSSING_PENALTY_S,
    DEFAULT_BIKE_SPEED,
    DRIVE_ON_LEFT,
    GRAPH_CACHE,
    JUNCTION_CROSSING_EXPOSURE_S,
    JUNCTION_EQUIV_LEN_M,
    JUNCTION_RIGHT_TURN_EXPOSURE_S,
    JUNCTION_RISK_FLOOR,
    JUNCTION_SIGNAL_EXPOSURE_S,
    JUNCTION_STOP_EXPOSURE_S,
    LAMBDA_SWEEP,
    RIGHT_TURN_EXTRA_S,
    SHARP_ANGLE,
    SHARP_TURN_PENALTY_S,
    SNAP_RADIUS_M,
    STRAIGHT_ANGLE,
    STOP_PENALTY_S,
    TRAFFIC_SIGNAL_PENALTY_S,
    TURN_PENALTY_S,
)
from safety import lts_from_risk

_R_EARTH_M = 6_371_008.8
_MAX_SPEED_MPS = max(DEFAULT_BIKE_SPEED.values()) / 3.6
_UTURN_PENALTY_S = SHARP_TURN_PENALTY_S * 2.5
_UTURN_ANGLE = 150.0
_GRID_DEG = 0.004


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _R_EARTH_M * math.asin(min(1.0, math.sqrt(a)))


def _project_param(lat, lon, la1, lo1, la2, lo2) -> float:
    """Fraction along segment 1->2 nearest to (lat, lon), clamped to [0, 1].

    Works in a local equirectangular frame, which is accurate well below the
    metre over the length of a single road segment.
    """
    k = math.cos(math.radians(lat))
    ax, ay = (lo2 - lo1) * k, la2 - la1
    px, py = (lon - lo1) * k, lat - la1
    denom = ax * ax + ay * ay
    if denom <= 0:
        return 0.0
    return max(0.0, min(1.0, (px * ax + py * ay) / denom))


def _slice_geom(geom, start_m: float, end_m: float):
    """Sub-polyline of ``geom`` between two arc-lengths, endpoints interpolated."""
    if not geom or end_m <= start_m:
        return []
    out = []
    acc = 0.0
    for i in range(len(geom) - 1):
        la1, lo1 = geom[i]
        la2, lo2 = geom[i + 1]
        seg = haversine_m(la1, lo1, la2, lo2)
        if seg <= 0:
            continue
        nxt = acc + seg
        if nxt >= start_m and acc <= end_m:
            lo_t = max(0.0, min(1.0, (start_m - acc) / seg))
            hi_t = max(0.0, min(1.0, (end_m - acc) / seg))
            if not out:
                out.append((la1 + lo_t * (la2 - la1), lo1 + lo_t * (lo2 - lo1)))
            out.append((la1 + hi_t * (la2 - la1), lo1 + hi_t * (lo2 - lo1)))
        acc = nxt
        if acc > end_m:
            break
    return out


def load_graph(path=None) -> dict:
    path = path or GRAPH_CACHE
    with open(path, "rb") as fh:
        return pickle.load(fh)


class Router:
    """Turn-aware A* over the routing graph, swept across the lambda range."""

    def __init__(self, graph: dict):
        self.g = graph
        self.n = len(graph["node_lat"])
        self.lat = graph["node_lat"]
        self.lon = graph["node_lon"]
        self.edge_u = graph["edge_u"]
        self.edge_v = graph["edge_v"]
        self.edge_len = graph["edge_len"]
        self.edge_time = graph["edge_time"]
        self.edge_lts = graph["edge_lts"]
        self.edge_risk = graph["edge_risk"]
        self.edge_pen = graph.get("edge_pen") or [0.0] * len(self.edge_u)
        self.edge_way = graph.get("edge_way") or [-1] * len(self.edge_u)
        self.edge_rev = graph["edge_rev"]
        self.bear_start = graph["edge_bear_start"]
        self.bear_end = graph["edge_bear_end"]
        self.geom_coords = graph["geom_coords"]
        self.geom_off = graph["geom_off"]
        self.adj = graph["adj"]
        self.comp = graph["comp"]
        self.main_comp = graph["main_comp"]
        self.pois = graph["pois"]
        # Banned turn pairs, keyed by the edge being entered.
        self.banned = {i: set(o) for i, o in graph.get("banned_turns", {}).items()}
        self._build_node_delay(graph)
        self._build_usability()
        self._build_grid()
        self._build_incoming()
        self._cache = {}

    # -- precomputation ----------------------------------------------------
    def _build_incoming(self) -> None:
        """Reverse adjacency, so edges *arriving* at a node can be found."""
        adj_in: list[list[int]] = [[] for _ in range(self.n)]
        for e in range(len(self.edge_u)):
            adj_in[self.edge_v[e]].append(e)
        self.adj_in = adj_in

    def _build_usability(self) -> None:
        """Flag vertices that can be both entered and left.

        Connectivity is computed on the undirected graph, so a vertex that only
        ever appears as the *end* of a one-way edge still looks reachable while
        having no way out. Snapping a trip endpoint onto such a vertex makes the
        search fail with a misleading "no connected route" message, so those
        vertices are ranked last.
        """
        has_out = [bool(a) for a in self.adj]
        has_in = [False] * self.n
        for edges in self.adj:
            for e in edges:
                has_in[self.edge_v[e]] = True
        self.usable = [has_out[i] and has_in[i] for i in range(self.n)]
    def _build_node_delay(self, graph: dict) -> None:
        delay = [0.0] * self.n
        exposure = [0.0] * self.n
        for idx in graph["signal_nodes"]:
            delay[idx] += TRAFFIC_SIGNAL_PENALTY_S
            exposure[idx] += JUNCTION_SIGNAL_EXPOSURE_S
        for idx in graph["stop_nodes"]:
            delay[idx] += STOP_PENALTY_S
            exposure[idx] += JUNCTION_STOP_EXPOSURE_S
        for idx in graph["crossing_nodes"]:
            delay[idx] += CROSSING_PENALTY_S
            exposure[idx] += JUNCTION_CROSSING_EXPOSURE_S
        self.node_delay = delay
        self.junc_exposure = exposure

        # A junction is as dangerous as the busiest road meeting at it, because
        # that is the traffic the rider has to negotiate. Take the max over
        # incident edges, then floor it: an uncontrolled junction is a conflict
        # point even when every road feeding it is quiet.
        risk = [0.0] * self.n
        edge_risk = self.edge_risk
        for e in range(len(self.edge_u)):
            r = edge_risk[e]
            u = self.edge_u[e]
            v = self.edge_v[e]
            if r > risk[u]:
                risk[u] = r
            if r > risk[v]:
                risk[v] = r
        for i in range(self.n):
            if exposure[i] and risk[i] < JUNCTION_RISK_FLOOR:
                risk[i] = JUNCTION_RISK_FLOOR
        self.junc_risk = risk

    def _build_grid(self) -> None:
        grid: dict[tuple[int, int], list[int]] = {}
        lat, lon = self.lat, self.lon
        for i in range(self.n):
            key = (int(lat[i] / _GRID_DEG), int(lon[i] / _GRID_DEG))
            bucket = grid.get(key)
            if bucket is None:
                grid[key] = [i]
            else:
                bucket.append(i)
        self.grid = grid

    # -- geometry ----------------------------------------------------------
    def edge_geom(self, e: int) -> list[tuple[float, float]]:
        a = self.geom_off[e]
        b = self.geom_off[e + 1]
        coords = self.geom_coords
        return [(coords[2 * i], coords[2 * i + 1]) for i in range(a, b)]

    # -- snapping ----------------------------------------------------------
    def _usable_edge(self, e: int) -> bool:
        """True when an edge can be entered at u and left at v."""
        return self.usable[self.edge_u[e]] and self.usable[self.edge_v[e]]

    def _edge_rank(self, e: int) -> int:
        """Rank an edge as a place to join the network, best first."""
        if self._usable_edge(e) and self.comp[self.edge_u[e]] == self.main_comp:
            return 0
        if self.comp[self.edge_u[e]] == self.main_comp:
            return 1
        if self._usable_edge(e):
            return 2
        return 3

    def _project_edge(self, e: int, lat: float, lon: float):
        """Closest point on an edge's polyline to (lat, lon).

        Returns ``(dist_m, along_m, total_m, point)`` where ``along_m`` is the
        distance from the edge's start to the projected point. Projecting onto
        the real polyline rather than the straight start-to-end chord matters on
        the many Bangkok sois that bend around a block.
        """
        coords = self.geom_coords
        a = self.geom_off[e]
        b = self.geom_off[e + 1]
        if b - a < 2:
            return None
        best = None
        total = 0.0
        along = 0.0
        for i in range(a, b - 1):
            la1, lo1 = coords[2 * i], coords[2 * i + 1]
            la2, lo2 = coords[2 * i + 2], coords[2 * i + 3]
            seg = haversine_m(la1, lo1, la2, lo2)
            if seg <= 0:
                continue
            t = _project_param(lat, lon, la1, lo1, la2, lo2)
            plat = la1 + t * (la2 - la1)
            plon = lo1 + t * (lo2 - lo1)
            d = haversine_m(lat, lon, plat, plon)
            if best is None or d < best[0]:
                best = (d, along + t * seg, plat, plon)
            along += seg
            total += seg
        if best is None:
            return None
        return best[0], best[1], total, (best[2], best[3])

    def snap_edge_options(self, lat: float, lon: float, radius_m: float = SNAP_RADIUS_M,
                          limit: int = 4) -> list[dict]:
        """Ranked places to join the network near (lat, lon), best first.

        A street can be geometrically closest yet useless: one-way pairings and
        service stubs leave small islands that no trip can actually reach. The
        caller walks this list until a route exists, so a trap edge costs a
        retry rather than a failed trip.
        """
        gx, gy = int(lat / _GRID_DEG), int(lon / _GRID_DEG)
        span = max(1, int(radius_m / (_GRID_DEG * 111_000)) + 1)
        seen: set[int] = set()
        found: list[tuple[int, float, dict]] = []

        def consider(e: int) -> None:
            if e in seen:
                return
            seen.add(e)
            proj = self._project_edge(e, lat, lon)
            if proj is None:
                return
            d, along, total, point = proj
            if d > radius_m:
                return
            found.append((self._edge_rank(e), d, {
                "edge": e,
                "dist_m": d,
                "along_m": along,
                "total_m": total,
                "point": point,
                "node": self.edge_u[e],
            }))

        for dx in range(-span, span + 1):
            for dy in range(-span, span + 1):
                for idx in self.grid.get((gx + dx, gy + dy), ()):
                    for e in self.adj[idx]:
                        consider(e)
                    # Edges whose *end* is in this cell are missed by adj alone.
                    for e in self.adj_in[idx]:
                        consider(e)

        found.sort(key=lambda t: (t[0], t[1]))
        out = [cand for _tier, _d, cand in found[:limit]]
        if not out:
            # No edge geometry in range: fall back to the nearest vertex.
            idx, d = self.snap(lat, lon, radius_m)
            if idx is not None:
                out.append({
                    "edge": -1,
                    "dist_m": d,
                    "along_m": 0.0,
                    "total_m": 0.0,
                    "point": (self.lat[idx], self.lon[idx]),
                    "node": idx,
                })
        return out

    def snap_edge(self, lat: float, lon: float, radius_m: float = SNAP_RADIUS_M):
        """Snap (lat, lon) onto the best point of a routable edge."""
        options = self.snap_edge_options(lat, lon, radius_m, limit=1)
        return options[0] if options else None

    def snap(self, lat: float, lon: float, radius_m: float = SNAP_RADIUS_M):
        """Return (node_index, distance_m) for the closest usable vertex.

        Candidates are ranked so that a vertex which can actually be entered and
        left, inside the main component, always wins over a closer dead end.
        """
        gx, gy = int(lat / _GRID_DEG), int(lon / _GRID_DEG)
        span = max(1, int(radius_m / (_GRID_DEG * 111_000)) + 1)
        # 0: usable + main component, 1: main component, 2: usable, 3: anything.
        best = [None] * 4
        best_d = [float("inf")] * 4
        for dx in range(-span, span + 1):
            for dy in range(-span, span + 1):
                for idx in self.grid.get((gx + dx, gy + dy), ()):
                    d = haversine_m(lat, lon, self.lat[idx], self.lon[idx])
                    if d > radius_m:
                        continue
                    in_main = self.comp[idx] == self.main_comp
                    if self.usable[idx] and in_main:
                        tier = 0
                    elif in_main:
                        tier = 1
                    elif self.usable[idx]:
                        tier = 2
                    else:
                        tier = 3
                    if d < best_d[tier]:
                        best[tier], best_d[tier] = idx, d
        for idx, d in zip(best, best_d):
            if idx is not None:
                return idx, d
        return None, None

    # -- costs -------------------------------------------------------------
    def turn_penalty(self, in_edge: int, out_edge: int) -> float:
        if in_edge < 0:
            return 0.0
        if self.edge_rev[out_edge] == in_edge:
            return _UTURN_PENALTY_S
        delta = (self.bear_start[out_edge] - self.bear_end[in_edge] + 540.0) % 360.0 - 180.0
        a = abs(delta)
        if a <= STRAIGHT_ANGLE:
            return 0.0
        if a >= _UTURN_ANGLE:
            return _UTURN_PENALTY_S
        base = SHARP_TURN_PENALTY_S if a >= SHARP_ANGLE else TURN_PENALTY_S
        turning_right = delta > 0
        if turning_right == DRIVE_ON_LEFT:
            return base + RIGHT_TURN_EXTRA_S
        return base

    def turn_exposure(self, in_edge: int, out_edge: int) -> float:
        """Seconds of conflict with oncoming traffic for this movement.

        Only a right turn across the flow (left-hand traffic) is exposed; going
        straight or turning left keeps the rider on the near side. U-turns are
        already charged heavily as time, so they add nothing here.
        """
        if in_edge < 0 or self.edge_rev[out_edge] == in_edge:
            return 0.0
        delta = (self.bear_start[out_edge] - self.bear_end[in_edge] + 540.0) % 360.0 - 180.0
        if abs(delta) <= STRAIGHT_ANGLE:
            return 0.0
        if (delta > 0) == DRIVE_ON_LEFT:
            return JUNCTION_RIGHT_TURN_EXPOSURE_S
        return 0.0

    # -- search ------------------------------------------------------------
    def _heuristic(self, node: int, dst: int) -> float:
        return haversine_m(self.lat[node], self.lon[node], self.lat[dst], self.lon[dst]) / _MAX_SPEED_MPS

    def _search(self, src: int, dst: int, lam: float):
        """A* between two vertices, returning edge indices or None."""
        out = self._search_multi({src: (0.0, [], 0.0)}, {dst: (0.0, [], 0.0)}, lam)
        return None if out is None else out[0]

    def _search_multi(self, starts: dict, ends: dict, lam: float):
        """A* from any start to any end, with per-endpoint connector costs.

        ``starts`` maps a vertex to ``(cost_to_reach_it, geometry_before_it)``
        and ``ends`` maps a vertex to ``(cost_from_it_to_the_target,
        geometry_after_it)``. The connectors let a trip begin and end partway
        along a street rather than being forced through a distant junction.

        Returns ``(edges, start_node, end_node)``, or ``None`` if unreachable.
        """
        key = (
            tuple(sorted((k, round(v[0], 6), round(v[2], 6)) for k, v in starts.items())),
            tuple(sorted((k, round(v[0], 6), round(v[2], 6)) for k, v in ends.items())),
            lam,
        )
        if key in self._cache:
            return self._cache[key]

        edge_v = self.edge_v
        edge_time = self.edge_time
        edge_risk = self.edge_risk
        edge_pen = self.edge_pen
        adj = self.adj
        delay = self.node_delay
        junc_expo = self.junc_exposure
        junc_risk = self.junc_risk
        banned = self.banned
        end_nodes = list(ends)
        lat, lon = self.lat, self.lon

        def h(node: int) -> float:
            return min(
                haversine_m(lat[node], lon[node], lat[t], lon[t]) for t in end_nodes
            ) / _MAX_SPEED_MPS

        best: dict[int, float] = {}
        parent: dict[int, tuple[int, int]] = {}
        pq = []
        for node, (c0, _geom, _dist) in starts.items():
            if c0 < best.get(node, float("inf")):
                best[node] = c0
                heapq.heappush(pq, (c0 + h(node), c0, node, -1))

        found = None
        while pq:
            _f, cost, node, in_edge = heapq.heappop(pq)
            prev = best.get(node)
            if prev is not None and cost > prev + 1e-9:
                continue
            if node in ends:
                found = (node, in_edge)
                break
            for e in adj[node]:
                v = edge_v[e]
                if v == node:
                    continue
                if in_edge >= 0 and e in banned.get(in_edge, ()):
                    continue
                step = edge_time[e] + edge_pen[e] + self.turn_penalty(in_edge, e)
                if v not in ends:
                    step += delay[v]
                step *= 1.0 + lam * edge_risk[e]
                # Junction safety: exposure seconds at the junction's risk, plus
                # the same for crossing oncoming traffic on the way in.
                if v not in ends:
                    expo = junc_expo[v] + self.turn_exposure(in_edge, e)
                    if expo:
                        step += lam * expo * junc_risk[v]
                nc = cost + step
                known = best.get(v)
                if known is not None and nc >= known - 1e-9:
                    continue
                best[v] = nc
                parent[v] = (node, e)
                heapq.heappush(pq, (nc + h(v), nc, v, e))

        if found is None:
            self._cache[key] = None
            return None

        end_node, _ = found
        edges: list[int] = []
        node = end_node
        while node in parent:
            pnode, e = parent[node]
            edges.append(e)
            node = pnode
        edges.reverse()
        result = (edges, node, end_node)
        self._cache[key] = result
        return result

    # -- endpoint connectors ----------------------------------------------
    def _edge_speed_mps(self, e: int) -> float:
        length = self.edge_len[e]
        return length / self.edge_time[e] if length > 0 and self.edge_time[e] > 0 else 4.0

    def _connector_starts(self, snap: dict) -> dict:
        """Ways to enter the network from a snapped point.

        Leaving the projection towards the edge's end runs *with* the edge's
        direction and is always legal; heading back to its start runs against
        the flow, so it is only offered when the street is two-way.

        Values are ``(time_s, geometry, distance_m)``.
        """
        e = snap["edge"]
        if e < 0:
            return {snap["node"]: (0.0, [], 0.0)}
        u, v = self.edge_u[e], self.edge_v[e]
        along, total = snap["along_m"], snap["total_m"]
        speed = self._edge_speed_mps(e)
        geom = self.edge_geom(e)
        starts: dict[int, tuple] = {}
        # Forward: projection -> v, riding along the edge.
        starts[v] = (
            (total - along) / speed,
            _slice_geom(geom, along, total) + [(self.lat[v], self.lon[v])],
            total - along,
        )
        # Backward: projection -> u, only when the street is two-way.
        if self.edge_rev[e] >= 0:
            starts[u] = (
                along / speed,
                list(reversed(_slice_geom(geom, 0.0, along))) + [(self.lat[u], self.lon[u])],
                along,
            )
        return starts

    def _connector_ends(self, snap: dict) -> dict:
        """Ways to leave the network for a snapped point.

        Values are ``(time_s, geometry, distance_m)``.
        """
        e = snap["edge"]
        if e < 0:
            return {snap["node"]: (0.0, [], 0.0)}
        u, v = self.edge_u[e], self.edge_v[e]
        along, total = snap["along_m"], snap["total_m"]
        speed = self._edge_speed_mps(e)
        geom = self.edge_geom(e)
        ends: dict[int, tuple] = {}
        # Arriving at u then riding to the projection: with the flow.
        ends[u] = (along / speed, [(self.lat[u], self.lon[u])] + _slice_geom(geom, 0.0, along), along)
        # Arriving at v then riding back: against the flow, so two-way only.
        if self.edge_rev[e] >= 0:
            ends[v] = (
                (total - along) / speed,
                [(self.lat[v], self.lon[v])] + list(reversed(_slice_geom(geom, along, total))),
                total - along,
            )
        return ends

    def _snap_lts(self, snap: dict) -> int:
        e = snap["edge"]
        return 1 if e < 0 else int(round(self.edge_lts[e]))

    def _snap_risk(self, snap: dict) -> float:
        e = snap["edge"]
        return 0.0 if e < 0 else self.edge_risk[e]

    def _snap_reason(self, snap: dict) -> str:
        e = snap["edge"]
        return "street stub" if e < 0 else self.g["edge_reason"][e]

    def _assemble(self, edges, start_node, end_node, starts, ends, o_snap, d_snap):
        """Stitch connector stubs onto the searched path.

        Returns ``(points, levels, extra)`` where ``extra`` carries the stub
        time, length and stress so the reported metrics match the drawn line.
        """
        pts, lts = self.path_geometry_lts(edges)
        o_geom = starts[start_node][1]
        d_geom = ends[end_node][1]
        o_level = self._snap_lts(o_snap)
        d_level = self._snap_lts(d_snap)

        out_pts: list = []
        out_lts: list = []

        def push(point, level):
            if out_pts and out_pts[-1] == point:
                return
            out_pts.append(point)
            out_lts.append(level)

        for p in o_geom:
            push(p, o_level)
        for p, level in zip(pts, lts):
            push(p, level)
        for p in d_geom:
            push(p, d_level)

        o_time, _, o_dist = starts[start_node]
        d_time, _, d_dist = ends[end_node]
        extra = {
            "time_s": o_time + d_time,
            "length_m": o_dist + d_dist,
            "penalty_s": 0.0,
            "legs": [
                (o_dist, self._snap_lts(o_snap), self._snap_risk(o_snap), self._snap_reason(o_snap)),
                (d_dist, self._snap_lts(d_snap), self._snap_risk(d_snap), self._snap_reason(d_snap)),
            ],
        }
        return out_pts, out_lts, extra

    def _direct_on_edge(self, o: dict, d: dict):
        """The sub-segment between two points on the same street, if legal."""
        e = o["edge"]
        if e < 0 or e != d["edge"]:
            return None
        a, b = o["along_m"], d["along_m"]
        total = o["total_m"]
        geom = self.edge_geom(e)
        if b >= a:
            pts = _slice_geom(geom, a, b)
        elif self.edge_rev[e] >= 0:
            pts = list(reversed(_slice_geom(geom, b, a)))
        else:
            return None
        if len(pts) < 2:
            return None
        length = abs(b - a)
        return {
            "edges": [e],
            "geometry": pts,
            "length_m": length,
            "lts": self.edge_lts[e],
            "risk": self.edge_risk[e],
            "time_s": length / self._edge_speed_mps(e),
            "penalty_s": self.edge_pen[e] * (length / total if total > 0 else 1.0),
            "reason": self.g["edge_reason"][e],
        }

    # -- metrics -----------------------------------------------------------
    def route_metrics(self, edges: list[int], lam: float, extra=None) -> dict:
        time_s = 0.0
        length_m = 0.0
        exposure = 0.0
        penalty_s = 0.0
        junction_risk_s = 0.0
        junction_n = 0
        # Stress length includes the junction equivalence, distance does not.
        junction_len = 0.0
        lts_km: dict[int, float] = {}
        reason_m: dict[str, float] = {}
        prev = -1
        for e in edges:
            time_s += self.edge_time[e]
            length_m += self.edge_len[e]
            penalty_s += self.edge_pen[e]
            exposure += self.edge_len[e] * self.edge_risk[e]
            bucket = int(round(self.edge_lts[e]))
            lts_km[bucket] = lts_km.get(bucket, 0.0) + self.edge_len[e]
            reason = self.g["edge_reason"][e]
            reason_m[reason] = reason_m.get(reason, 0.0) + self.edge_len[e]
            time_s += self.turn_penalty(prev, e)
            prev = e
        prev = edges[0] if edges else -1
        for e in edges[1:]:
            v = self.edge_u[e]
            time_s += self.node_delay[v]
            expo = self.junc_exposure[v] + self.turn_exposure(prev, e)
            prev = e
            if expo <= 0.0:
                continue
            risk = self.junc_risk[v]
            junction_n += 1
            junction_risk_s += expo * risk
            equiv_m = JUNCTION_EQUIV_LEN_M * (expo / JUNCTION_CROSSING_EXPOSURE_S)
            junction_len += equiv_m
            jbucket = int(round(lts_from_risk(risk)))
            lts_km[jbucket] = lts_km.get(jbucket, 0.0) + equiv_m

        # Connector stubs are part of the ride even though they are not edges.
        if extra:
            time_s += extra.get("time_s", 0.0)
            length_m += extra.get("length_m", 0.0)
            penalty_s += extra.get("penalty_s", 0.0)
            for leg_m, leg_lts, leg_risk, leg_reason in extra.get("legs", ()):
                if leg_m <= 0:
                    continue
                bucket = int(round(leg_lts))
                lts_km[bucket] = lts_km.get(bucket, 0.0) + leg_m
                reason_m[leg_reason] = reason_m.get(leg_reason, 0.0) + leg_m
                exposure += leg_m * leg_risk

        if length_m <= 0:
            return None

        # Junction exposure expressed in the same metre-risk units as link
        # exposure, via the equivalence already used for `junction_len`. Keeping
        # the dominance axis in these units stops a route being discarded as
        # "dominated" on links while actually being calmer once its junctions
        # are counted -- which would make the front inconsistent with the cost
        # the search itself minimises.
        junction_exposure = junction_risk_s * (JUNCTION_EQUIV_LEN_M / JUNCTION_CROSSING_EXPOSURE_S)

        stress_len = length_m + junction_len
        avg_lts = sum(k * v for k, v in lts_km.items()) / stress_len
        max_lts = max(lts_km) if lts_km else 1
        low_stress_m = sum(v for k, v in lts_km.items() if k <= 2)
        comfort = low_stress_m / stress_len

        top_reasons = sorted(reason_m.items(), key=lambda kv: -kv[1])[:5]

        return {
            "lambda": lam,
            "time_s": round(time_s, 1),
            "distance_m": round(length_m, 1),
            "exposure": round(exposure, 1),
            "stress_exposure": round(exposure + junction_exposure, 1),
            "junction_count": junction_n,
            "junction_risk_s": round(junction_risk_s, 1),
            "avg_lts": round(avg_lts, 2),
            "max_lts": max_lts,
            "comfort_pct": round(comfort * 100.0, 1),
            "safety_score": round(max(0.0, min(100.0, 100.0 * (4.0 - avg_lts) / 3.0)), 1),
            "lts_km": {str(k): round(v / 1000.0, 2) for k, v in sorted(lts_km.items())},
            "reasons": [{"reason": r, "km": round(m / 1000.0, 2)} for r, m in top_reasons],
            "edges": edges,
        }

    # -- public API --------------------------------------------------------
    def plan(self, o_lat, o_lon, d_lat, d_lon, lambdas=LAMBDA_SWEEP) -> dict:
        o_opts = self.snap_edge_options(o_lat, o_lon)
        d_opts = self.snap_edge_options(d_lat, d_lon)
        if not o_opts or not d_opts:
            raise ValueError(
                f"no routable road within {int(SNAP_RADIUS_M)} m of one of the endpoints"
            )

        o_snap, d_snap, candidates = self._resolve(o_opts, d_opts, lambdas)

        result = {
            "origin": {"lat": o_lat, "lon": o_lon, "snap_m": round(o_snap["dist_m"], 1)},
            "destination": {"lat": d_lat, "lon": d_lon, "snap_m": round(d_snap["dist_m"], 1)},
            "routes": [],
            "note": None,
        }
        if not candidates:
            result["note"] = "no connected route between those points"
            return result

        result["routes"] = _drop_duplicates(_pareto_front(candidates))
        for i, r in enumerate(result["routes"]):
            r["index"] = i
            pts = r.pop("_pts")
            lts = r.pop("_lts")
            r["geometry"] = [[round(la, 6), round(lo, 6)] for la, lo in pts]
            r["geometry_lts"] = lts
        return result

    def _resolve(self, o_opts, d_opts, lambdas, tries: int = 3):
        """Find the best pair of snap candidates that actually yields a route.

        The closest street is not always the usable one, so the second and
        third candidates are tried on both ends before giving up.
        """
        attempts = [(o_opts[0], d_opts[0])]
        for alt in o_opts[1:tries]:
            attempts.append((alt, d_opts[0]))
        for alt in d_opts[1:tries]:
            attempts.append((o_opts[0], alt))
        for o_snap, d_snap in attempts:
            candidates = self._candidates(o_snap, d_snap, lambdas)
            if candidates:
                return o_snap, d_snap, candidates
        return attempts[0][0], attempts[0][1], []

    def _candidates(self, o_snap: dict, d_snap: dict, lambdas) -> list[dict]:
        """Every distinct path worth offering for this trip, one per lambda."""
        direct = self._direct_on_edge(o_snap, d_snap)
        if direct is not None:
            # Both endpoints sit on the same street, so riding that street is
            # the answer; sending the search out to a junction and back is not.
            length = direct["length_m"]
            extra = {
                "time_s": direct["time_s"],
                "length_m": length,
                "penalty_s": direct["penalty_s"],
                "legs": [(length, direct["lts"], direct["risk"], direct["reason"])],
            }
            metrics = self.route_metrics([], lambdas[0] if lambdas else 0.0, extra=extra)
            if metrics is None:
                return []
            metrics["_pts"] = direct["geometry"]
            metrics["_lts"] = [int(round(direct["lts"]))] * len(direct["geometry"])
            return [metrics]

        starts = self._connector_starts(o_snap)
        ends = self._connector_ends(d_snap)
        seen: set[tuple[int, ...]] = set()
        out: list[dict] = []
        for lam in lambdas:
            found = self._search_multi(starts, ends, lam)
            if not found:
                continue
            edges, start_node, end_node = found
            sig = tuple(edges)
            if sig in seen:
                continue
            seen.add(sig)
            pts, lts, extra = self._assemble(
                edges, start_node, end_node, starts, ends, o_snap, d_snap
            )
            metrics = self.route_metrics(edges, lam, extra=extra)
            if metrics is None:
                continue
            metrics["_pts"] = pts
            metrics["_lts"] = lts
            out.append(metrics)
        return out

    def path_geometry_lts(self, edges: list[int]) -> tuple[list[tuple[float, float]], list[int]]:
        """Trace the path, returning points and the stress level at each one.

        The UI colours the line by these levels so riders can see exactly which
        stretch of the trip is the exposed part.
        """
        pts: list[tuple[float, float]] = []
        lts: list[int] = []
        for e in edges:
            geom = self.edge_geom(e)
            if not geom:
                continue
            level = int(round(self.edge_lts[e]))
            if pts and pts[-1] == geom[0]:
                pts.extend(geom[1:])
                lts.extend([level] * (len(geom) - 1))
            else:
                pts.extend(geom)
                lts.extend([level] * len(geom))
        return pts, lts

    def path_geometry(self, edges: list[int]) -> list[list[float]]:
        pts, _ = self.path_geometry_lts(edges)
        return [[round(la, 6), round(lo, 6)] for la, lo in pts]


def _pareto_front(candidates: list[dict]) -> list[dict]:
    """Keep routes that no other route beats on both time and stress exposure.

    The dominance axis is ``stress_exposure`` -- link stress plus junction
    stress converted to the same units -- so the front agrees with the stress
    the UI reports. Link-only exposure would let a route be dropped as
    dominated while it was in fact the calmer ride once junctions counted.
    """
    ordered = sorted(candidates, key=lambda r: (r["time_s"], r["stress_exposure"]))
    front = []
    best_exposure = float("inf")
    for r in ordered:
        if r["stress_exposure"] < best_exposure - 1e-9:
            front.append(r)
            best_exposure = r["stress_exposure"]
    return front


def _drop_duplicates(front: list[dict]) -> list[dict]:
    """Collapse routes that are effectively the same ride.

    The lambda sweep can converge on the same corridor twice with a marginal
    time or stress difference; showing both adds noise rather than choice.
    """
    kept: list[dict] = []
    for r in front:
        dup = None
        for i, k in enumerate(kept):
            dt = abs(r["time_s"] - k["time_s"]) / max(k["time_s"], 1.0)
            de = abs(r["stress_exposure"] - k["stress_exposure"]) / max(k["stress_exposure"], 1.0)
            if dt <= 0.06 and de <= 0.08 and abs(r["avg_lts"] - k["avg_lts"]) <= 0.25:
                dup = i
                break
        if dup is None:
            kept.append(r)
        elif r["stress_exposure"] < kept[dup]["stress_exposure"]:
            kept[dup] = r
    return kept


def _fmt(seconds: float) -> str:
    m, s = divmod(int(round(seconds)), 60)
    return f"{m} min {s:02d} s"


def main() -> None:
    if len(sys.argv) < 5:
        print("usage: router.py OLAT OLON DLAT DLON")
        sys.exit(2)
    o_lat, o_lon, d_lat, d_lon = (float(x) for x in sys.argv[1:5])
    router = Router(load_graph())
    res = router.plan(o_lat, o_lon, d_lat, d_lon)
    print(f"snap: origin {res['origin']['snap_m']} m, destination {res['destination']['snap_m']} m")
    if res["note"]:
        print("note:", res["note"])
    print(f"\n{len(res['routes'])} Pareto-optimal route(s):")
    for r in res["routes"]:
        print(
            f"  [{r['index']}] lambda={r['lambda']:<5} {_fmt(r['time_s']):>12}  "
            f"{r['distance_m'] / 1000:5.2f} km  avgLTS {r['avg_lts']:.2f}  "
            f"maxLTS {r['max_lts']}  calm {r['comfort_pct']:5.1f}%  "
            f"score {r['safety_score']:5.1f}  pts {len(r['geometry'])}"
        )
        for reason in r["reasons"][:3]:
            print(f"        {reason['km']:5.2f} km  {reason['reason']}")


if __name__ == "__main__":
    main()
