#!/usr/bin/env bash
# Download the OSM extract and build the routing graph.
#
# Safe to re-run: every step is skipped when its output already exists.
#
#   ./scripts/setup_data.sh
#
# Needs `osmium-tool` (the CLI, not the Python package) to clip the country
# extract down to the area the router covers.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA="$ROOT/data"
PBF="$DATA/bangkok.osm.pbf"
COUNTRY="${COUNTRY_PBF:-$DATA/thailand-latest.osm.pbf}"

# BBOX in src/config.py is (south, west, north, east) = 13.64,100.40,13.86,100.66.
# osmium wants min_lon,min_lat,max_lon,max_lat. PAD_DEG in build_graph.py reads
# nodes slightly outside BBOX so ways crossing the edge keep their geometry, so
# the clip has to include that margin or boundary ways lose their end nodes.
EXTRACT_BBOX="100.3880,13.6280,100.6720,13.8720"
GEOFABRIK="https://download.geofabrik.de/asia/thailand-latest.osm.pbf"

PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"

mkdir -p "$DATA"

if [ ! -f "$PBF" ]; then
  if ! command -v osmium >/dev/null 2>&1; then
    echo "error: 'osmium' not found. Install osmium-tool:" >&2
    echo "  macOS:  brew install osmium-tool" >&2
    echo "  Debian: sudo apt-get install -y osmium-tool" >&2
    exit 1
  fi

  if [ ! -f "$COUNTRY" ]; then
    echo "==> downloading Thailand extract (~313 MB) from Geofabrik"
    curl -L --fail --progress-bar -o "$COUNTRY.part" "$GEOFABRIK"
    mv "$COUNTRY.part" "$COUNTRY"
  else
    echo "==> reusing $COUNTRY"
  fi

  echo "==> clipping to $EXTRACT_BBOX"
  # Write to .part and move, so an interrupted clip can never leave a
  # truncated file that the next run would treat as already-done.
  rm -f "$PBF.part"
  osmium extract --overwrite --bbox "$EXTRACT_BBOX" -o "$PBF.part" "$COUNTRY"
  mv "$PBF.part" "$PBF"
else
  echo "==> reusing $PBF"
fi

echo "==> building graph"
"$PY" "$ROOT/src/build_graph.py" "$@"

echo
echo "done. start the server with:  $PY $ROOT/src/server.py"
