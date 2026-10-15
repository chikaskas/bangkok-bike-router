"""HTTP API and static host for the Bangkok bicycle router.

Pure stdlib: the machine this targets has no Java or Node.js, so the routing
engine is in-process Python and the UI is plain static files.

    ./.venv/bin/python src/server.py [--port 8000] [--host 127.0.0.1]

Endpoints
    GET /api/stats                 graph summary
    GET /api/search?q=&limit=      place autocomplete
    GET /api/route?from=&to=       plan between two place names or lat/lon
    GET /api/route?from_lat=&...   plan between explicit coordinates
    GET /api/health                liveness probe

Route options
    &lam=1.6                       plan for one exact safety/time trade-off
    &lams=0,0.8,3,12               plan for an explicit list of trade-offs
    (neither)                      plan the default sweep, for comparison
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import LAMBDA_SWEEP, MAX_LAMBDA, SERVER_HOST, SERVER_PORT, WEB_DIR  # noqa: E402
from geocode import Geocoder  # noqa: E402
from router import Router, load_graph  # noqa: E402

STATE: dict = {}


def _lambdas(q: dict) -> list[float]:
    """Read the trade-off values to plan for.

    ``lam`` asks for one exact value (the UI slider), ``lams`` for a list. With
    neither, the full sweep runs so the client can compare alternatives.
    """
    single = (q.get("lam") or [None])[0]
    if single is not None:
        raw = [single]
    else:
        many = (q.get("lams") or [None])[0]
        if many is None:
            return list(LAMBDA_SWEEP)
        raw = many.split(",")

    values = []
    for item in raw:
        try:
            lam = float(item)
        except (TypeError, ValueError):
            raise ValueError(f"lambda must be a number, got {item!r}")
        values.append(max(0.0, min(MAX_LAMBDA, lam)))
    if not values:
        raise ValueError("lambda list is empty")
    return values[:64]


def _route_key(edges) -> str:
    """Stable id for a path, so the UI can skip redrawing an unchanged route."""
    digest = hashlib.sha1(",".join(str(e) for e in edges).encode("ascii"))
    return digest.hexdigest()[:12]


def _coord_pair(q: dict, prefix: str):
    """Read `<prefix>_lat` / `<prefix>_lon` as floats, or None if absent."""
    lat, lon = q.get(f"{prefix}_lat"), q.get(f"{prefix}_lon")
    if not lat or not lon:
        return None
    try:
        return float(lat[0]), float(lon[0])
    except (TypeError, ValueError):
        raise ValueError(f"{prefix}_lat and {prefix}_lon must be numbers")


def _endpoint(q: dict, prefix: str, geo: Geocoder):
    """Resolve one trip endpoint from coordinates or a free-text place name."""
    pair = _coord_pair(q, prefix)
    if pair:
        return pair[0], pair[1], f"{pair[0]:.5f}, {pair[1]:.5f}"

    text = (q.get(prefix) or [""])[0].strip()
    if not text:
        raise ValueError(f"missing '{prefix}' (a place name or lat/lon)")
    resolved = geo.resolve(text)
    if not resolved:
        raise ValueError(f"could not find a place matching '{text}'")
    return resolved


def _route_response(q: dict) -> dict:
    geo, router = STATE["geo"], STATE["router"]
    o_lat, o_lon, o_label = _endpoint(q, "from", geo)
    d_lat, d_lon, d_label = _endpoint(q, "to", geo)

    result = router.plan(o_lat, o_lon, d_lat, d_lon, lambdas=_lambdas(q))
    # Edge ids are internal; the UI only needs geometry, metrics and an identity.
    for route in result["routes"]:
        route["key"] = _route_key(route["edges"])
        route.pop("edges", None)
    result["origin"]["label"] = o_label
    result["destination"]["label"] = d_label
    return result


def _search_response(q: dict) -> dict:
    text = (q.get("q") or [""])[0].strip()
    if not text:
        return {"query": "", "results": []}
    try:
        limit = max(1, min(40, int((q.get("limit") or ["12"])[0])))
    except ValueError:
        limit = 12
    return {"query": text, "results": STATE["geo"].search(text, limit=limit)}


class Handler(BaseHTTPRequestHandler):
    server_version = "BikeRouter/0.1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quieter default logging
        if self.path.startswith("/api/route"):
            sys.stderr.write("  %s\n" % (fmt % args))

    # -- helpers -----------------------------------------------------------
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: dict, status: int = 200) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status=status)

    def _static(self, path: str) -> None:
        rel = unquote(path).lstrip("/") or "index.html"
        target = (WEB_DIR / rel).resolve()
        # Never serve anything outside web/.
        if not target.is_file() or WEB_DIR.resolve() not in target.parents:
            self._error(404, "not found")
            return
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self._send(200, target.read_bytes(), ctype)

    # -- verbs -------------------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        try:
            if path == "/api/health":
                self._json({"ok": True})
            elif path == "/api/stats":
                self._json(STATE["stats"])
            elif path == "/api/search":
                self._json(_search_response(query))
            elif path == "/api/route":
                self._json(_route_response(query))
            elif path.startswith("/api/"):
                self._error(404, f"unknown endpoint {path}")
            else:
                self._static(path)
        except ValueError as exc:
            self._error(400, str(exc))
        except Exception as exc:  # noqa: BLE001 - surface the reason to the client
            traceback.print_exc()
            self._error(500, f"{type(exc).__name__}: {exc}")

    do_HEAD = do_GET


def _stats(graph: dict) -> dict:
    stats = dict(graph["stats"])
    bbox = graph["bbox"]
    stats["bbox"] = list(bbox)
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=SERVER_HOST)
    parser.add_argument("--port", type=int, default=SERVER_PORT)
    args = parser.parse_args()

    graph = load_graph()
    STATE["router"] = Router(graph)
    STATE["geo"] = Geocoder(STATE["router"])
    STATE["stats"] = _stats(graph)
    print(f"loaded graph: {len(graph['node_lat']):,} nodes, "
          f"{len(graph['edge_u']):,} edges, {len(STATE['geo'].places):,} named places")

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    print(f"serving on http://{args.host}:{args.port}/  (ctrl-c to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
