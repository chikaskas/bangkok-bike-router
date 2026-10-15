#!/usr/bin/env bash
# Codespaces / devcontainer setup.
#
# Installs the build-time dependency (osmium-tool) and the Python packages,
# then builds the routing graph if the OSM extract is present. The extract
# itself is not committed, so run scripts/setup_data.sh to fetch it — that
# step is a ~313 MB download and is deliberately left opt-in rather than
# running on every container start.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> installing osmium-tool (needed to clip the OSM extract)"
sudo apt-get update -qq
sudo apt-get install -y -qq osmium-tool

echo "==> installing python packages"
pip install --quiet -r "$ROOT/requirements.txt"

if [ -f "$ROOT/data/bangkok.osm.pbf" ]; then
  echo "==> building graph"
  python "$ROOT/src/build_graph.py"
else
  echo
  echo "The OSM extract is not present (it is not committed — data/ is ~500 MB)."
  echo "Run this once to download it and build the graph:"
  echo
  echo "    ./scripts/setup_data.sh"
  echo
  echo "Then start the server:"
  echo
  echo "    python src/server.py"
fi
