#!/bin/sh
# One-shot acceptance entrypoint for the `verify` compose service.
#
#   1. build check: byte-compile every Python source file
#   2. code tests:  solver + API unit tests
#   3. API/HTTP smoke against the live `audit` service
#
# Exits 0 only if every stage passes; the compose service is expected to run
# to completion and report acceptance via its exit code.
set -u

# Project root = parent of this script's directory (/app in the image).
cd "$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
FAILED=0

echo "=== [1/3] build check: byte-compiling sources ==="
if python3 -m compileall -q app tests verify; then
    echo "build check OK"
else
    echo "build check FAILED"
    FAILED=1
fi

echo "=== [2/3] code tests: unit + API ==="
if python3 -m unittest discover -s tests -v; then
    echo "code tests OK"
else
    echo "code tests FAILED"
    FAILED=1
fi

echo "=== [3/3] API/HTTP smoke against ${BASE_URL:-http://audit:8080} ==="
if python3 verify/smoke.py; then
    echo "smoke OK"
else
    echo "smoke FAILED"
    FAILED=1
fi

if [ "$FAILED" -eq 0 ]; then
    echo "ACCEPTANCE: PASS"
else
    echo "ACCEPTANCE: FAIL"
fi
exit "$FAILED"
