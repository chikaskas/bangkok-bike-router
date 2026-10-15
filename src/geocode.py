"""Place lookup over the POI gazetteer extracted from the OSM data.

The gazetteer is small (a few thousand named places), so scoring is done with
plain string work plus difflib rather than an external search index.
"""
from __future__ import annotations

import difflib
import re
import sys
import unicodedata

from router import Router, haversine_m, load_graph

_WORD = re.compile(r"[a-z0-9]+")
_LATLON = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*[,;]\s*(-?\d+(?:\.\d+)?)\s*$")

# Bangkok's densest area (Siam). Used only to break exact score ties, so that
# "Wat Pho" resolves to the central temple rather than a namesake 12 km out.
_CENTER_LAT = 13.7466
_CENTER_LON = 100.5347

# Kinds that make good cycling endpoints, nudged up the ranking.
_KIND_BOOST = {
    "district": 8.0,
    "street": 4.0,
    "transit station": 6.0,
    "transit entrance": 4.0,
    "ferry pier": 6.0,
    "bus station": 5.0,
    "mall": 5.0,
    "university": 5.0,
    "market": 4.0,
    "park": 3.0,
    "hospital": 3.0,
    "temple": 3.0,
    "attraction": 3.0,
    "hotel": 2.0,
    "school": 1.0,
    "government office": 1.0,
    "sport": 1.0,
    "shop": -2.0,
    "cafe": -2.0,
    "restaurant": -2.0,
    "bank": -2.0,
    "airport": 6.0,
    "bike shop": 5.0,
    "bike parking": 5.0,
    "canal": 4.0,
    "clinic": 3.0,
    "civic": 3.0,
    "historic": 3.0,
    "nature": 3.0,
    "residential": 3.0,
    "pharmacy": 2.0,
    "building": 2.0,
    "landuse": 2.0,
    "landmark": 2.0,
    "entertainment": 2.0,
    "bus stop": 1.0,
    "office": 0.0,
    "fuel": -1.0,
    "parking": -1.0,
    "taxi": -1.0,
    "bar": -3.0,
}


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "").casefold()
    text = text.replace("'", "").replace("&", " and ")
    return " ".join(_WORD.findall(text))


def parse_latlon(text: str):
    m = _LATLON.match(text or "")
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    if -90 <= lat <= 90 and -180 <= lon <= 180:
        return lat, lon
    return None


def _score(query: str, name: str) -> float:
    if not query or not name:
        return 0.0
    if name == query:
        return 120.0
    if name.startswith(query):
        # Prefer tighter matches: "siam paragon" beats "siam paragon annex".
        return 100.0 - min(15.0, (len(name) - len(query)) * 0.4)
    q_tokens = query.split()
    n_tokens = name.split()
    if q_tokens and all(any(t.startswith(qt) for t in n_tokens) for qt in q_tokens):
        return 85.0 - min(10.0, (len(n_tokens) - len(q_tokens)) * 0.5)
    if query in name:
        return 70.0
    # Loose fuzzy matching only when the strings genuinely overlap.
    ratio = difflib.SequenceMatcher(None, query, name).ratio()
    if ratio < 0.75:
        return 0.0
    if q_tokens and not any(
        t.startswith(qt[:2]) for qt in q_tokens if len(qt) >= 2 for t in n_tokens
    ):
        return 0.0
    return 40.0 * ratio


def _name_variants(poi: dict) -> list[str]:
    out = []
    for raw in (poi["name"], poi.get("alt"), *poi.get("alts", ())):
        if not raw:
            continue
        norm = normalize(raw)
        if not norm:
            # Thai-only names normalise to nothing; match them verbatim instead.
            norm = " ".join(raw.casefold().split())
        if norm and norm not in out:
            out.append(norm)
    return out


class Geocoder:
    def __init__(self, router: Router):
        self.router = router
        self.places = []
        for p in router.pois:
            variants = _name_variants(p)
            if not variants:
                continue
            self.places.append(
                {
                    "name": p["name"],
                    "alt": p.get("alt"),
                    "kind": p["kind"],
                    "lat": p["lat"],
                    "lon": p["lon"],
                    "variants": variants,
                }
            )
        self._add_streets(router.g)

    def _add_streets(self, graph: dict) -> None:
        """Index street names too, so "Khao San Road" resolves to the road."""
        names = graph["edge_name"]
        alts = graph.get("edge_name_alt") or [""] * len(names)
        lat, lon = graph["node_lat"], graph["node_lon"]
        u, v, length = graph["edge_u"], graph["edge_v"], graph["edge_len"]

        # name -> [display, weight, sum_lat*w, sum_lon*w, alt]
        acc: dict[str, list] = {}
        for e, raw in enumerate(names):
            for text, is_alt in ((raw, False), (alts[e], True)):
                if not text:
                    continue
                norm = normalize(text)
                if not norm:
                    continue
                w = length[e]
                entry = acc.get(norm)
                if entry is None:
                    entry = acc[norm] = [text, 0.0, 0.0, 0.0, None]
                entry[1] += w
                entry[2] += (lat[u[e]] + lat[v[e]]) * 0.5 * w
                entry[3] += (lon[u[e]] + lon[v[e]]) * 0.5 * w
                if is_alt and not entry[4]:
                    entry[4] = text
                elif not is_alt and entry[0] != text and not entry[4]:
                    entry[4] = entry[0]

        for norm, (display, weight, slat, slon, alt) in acc.items():
            if weight <= 0:
                continue
            self.places.append(
                {
                    "name": display,
                    "alt": alt if alt and alt != display else None,
                    "kind": "street",
                    "lat": slat / weight,
                    "lon": slon / weight,
                    "variants": (
                        [norm]
                        if not alt or alt == display
                        else [norm, normalize(alt) or " ".join(alt.casefold().split())]
                    ),
                }
            )

    def search(self, query: str, limit: int = 8) -> list[dict]:
        q = normalize(query) or " ".join((query or "").casefold().split())
        if not q:
            return []
        q_tokens = q.split()

        scored = []
        for place in self.places:
            best = 0.0
            for variant in place["variants"]:
                s = _score(q, variant)
                if s > best:
                    best = s
            if best <= 0:
                continue
            best += _KIND_BOOST.get(place["kind"], 0.0)
            # Reward how many query tokens the name covers.
            tokens = {t for variant in place["variants"] for t in variant.split()}
            covered = sum(1 for qt in q_tokens if any(t.startswith(qt) for t in tokens))
            best += covered * 2.0
            scored.append((best, place))

        scored.sort(
            key=lambda sp: (
                -sp[0],
                haversine_m(_CENTER_LAT, _CENTER_LON, sp[1]["lat"], sp[1]["lon"]),
                sp[1]["name"],
            )
        )
        # Drop only genuinely hopeless tail matches. Fuzzy hits top out around
        # 40, so scaling the floor off an exact-match winner (120) used to land
        # above the whole fuzzy band -- a single typo collapsed the list to one
        # result. `_score` already rejects poor matches, so a low floor here is
        # safe and keeps near-misses visible.
        if scored:
            floor = max(12.0, 0.20 * scored[0][0])
            scored = [sp for sp in scored if sp[0] >= floor]
        scored = self._thin(scored, limit)

        out = []
        for s, place in scored:
            out.append(
                {
                    "name": place["name"],
                    "alt": place.get("alt"),
                    "kind": place["kind"],
                    "lat": round(place["lat"], 6),
                    "lon": round(place["lon"], 6),
                    "score": round(s, 1),
                }
            )
        return out

    @staticmethod
    def _thin(scored: list, limit: int) -> list:
        """Cap repeated names (Bangkok has hundreds of 7-Elevens)."""
        per_name: dict[str, int] = {}
        kept = []
        for s, place in scored:
            n = per_name.get(place["name"], 0)
            if n >= 2:
                continue
            per_name[place["name"]] = n + 1
            kept.append((s, place))
            if len(kept) >= limit:
                break
        return kept

    def resolve(self, text: str):
        """Return (lat, lon, label) for a coordinate string or a place name."""
        coords = parse_latlon(text)
        if coords:
            return coords[0], coords[1], f"{coords[0]:.5f}, {coords[1]:.5f}"
        hits = self.search(text, limit=1)
        if not hits:
            return None
        h = hits[0]
        return h["lat"], h["lon"], h["name"]


def main() -> None:
    query = " ".join(sys.argv[1:]) or "siam paragon"
    geo = Geocoder(Router(load_graph()))
    print(f"gazetteer: {len(geo.places):,} named places")
    for h in geo.search(query):
        alt = f"  [{h['alt']}]" if h.get("alt") else ""
        print(f"  {h['score']:6.1f}  {h['name']:<42}{alt} {h['kind']:<18} {h['lat']:.5f},{h['lon']:.5f}")


if __name__ == "__main__":
    main()
