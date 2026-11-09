#!/usr/bin/env sh
# Build dist/sessionboard.pyz: a single-file zipapp with all dependencies bundled.
#
# The result needs only a Python 3.11+ interpreter on the target machine: no pip,
# uv, PyPI or Artifactory access. Every dependency is pure Python, so one file
# runs on macOS, Linux and Windows alike.
#
# Usage: scripts/build-pyz.sh [python-version]   (default 3.11, the project floor)
set -eu

cd "$(dirname "$0")/.."
PY="${1:-3.11}"
STAGE="build/pyz"
OUT="dist/sessionboard.pyz"

rm -rf "$STAGE"
mkdir -p "$STAGE" dist

# Install the project and its locked dependencies into a flat directory. The
# .dist-info directories must stay: textual reads its version via importlib.metadata.
uv pip install --quiet --python "$PY" --target "$STAGE" .
rm -rf "$STAGE/bin"

python3 -m zipapp "$STAGE" \
    --output "$OUT" \
    --python "/usr/bin/env python3" \
    --main "sessionboard.cli:main" \
    --compress

rm -rf "$STAGE"
ls -lh "$OUT"
