#!/bin/sh
set -eu
REPO="${1:-$HOME/hermes-trading}"
cd "$REPO"
test -d hermes/market
cp "$(dirname "$0")/hermes/market/metrics.py" hermes/market/metrics.py
cp "$(dirname "$0")/tests/unit/test_metrics.py" tests/unit/test_metrics.py
echo "C7a core files installed into $REPO"
