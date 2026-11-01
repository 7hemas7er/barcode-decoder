#!/bin/sh
# T3, the decoder's tests. Needs Docker. From the repository root:
#
#   sh tests/run_t3.sh                 build the image, run the suite inside it, then the host suite
#   BUILD=0 sh tests/run_t3.sh         without building
#
# 1. Inside the image (Python 3.13, zxing-cpp, Pillow, pi-heif, poppler: what ships), read-only, as UID 10001,
#    tests mounted read-only. /tmp is a tmpfs with exec here only because tests/test_barcodes.py writes a fake
#    pdftoppm there; the service itself never runs anything from /tmp (its tmpfs is noexec).
# 2. On the host: tests/host (the container as documented, and the repository layout), with any Python 3 + PyYAML.
#
# Containers are named "$BCDECODER_CONTAINER_PREFIX-..." (default barcode-decoder-test) and removed at the end.
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
IMAGE=${BCDECODER_IMAGE:-local/barcode-decoder:1.0.0}
PREFIX=${BCDECODER_CONTAINER_PREFIX:-barcode-decoder-test}
PYTHON=${PYTHON:-python3}

if [ "${BUILD:-1}" = 1 ]; then
  docker build -t "$IMAGE" "$ROOT/barcode_decoder"
fi

status=0
docker run --rm --name "$PREFIX-unit-$$" --read-only --user 10001 --tmpfs /tmp:rw,exec,size=1g \
  --memory 3g --pids-limit 512 --cap-drop ALL --security-opt no-new-privileges --network none \
  -v "$ROOT/tests:/tests:ro" "$IMAGE" \
  python -I -B -m unittest discover -s /tests -t /tests -v || status=$?

BCDECODER_IMAGE="$IMAGE" BCDECODER_CONTAINER_PREFIX="$PREFIX" \
  "$PYTHON" -I -B -m unittest discover -s "$ROOT/tests/host" -t "$ROOT/tests/host" -v || status=$?

exit $status
