"""The limits of the decoder. Each one can be lowered (or moved) with an environment variable, for tests and for
unusual installs; the defaults are the API v1 values that GET /health reports.

Not named config.py: the Supervisor reads every config.* file of an app repository as an app.
"""
import os

MIB = 1 << 20


def _int(name, default, low, high):
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise SystemExit(f"{name}={raw!r}: not an integer") from None
    if not low <= value <= high:
        raise SystemExit(f"{name}={value}: outside {low}..{high}")
    return value


HOST = os.environ.get("BCDECODER_HOST") or "0.0.0.0"
PORT = _int("BCDECODER_PORT", 8099, 0, 65535)            # 0: any free port (tests); the start line says which

MAX_BYTES = _int("BCDECODER_MAX_BYTES", 32 * MIB, 1, 256 * MIB)   # Content-Length above it: 413, body not read
MAX_PIXELS = 50_000_000                                  # header width x height above it: 422 (an iPhone 48 Mpx passes)
MAX_PAGES = 10                                           # the most PDF pages a request may ask for
DEFAULT_PAGES = 3                                        # PDF pages rendered when the request does not say
PDF_SIDE = 2400                                          # pixels on the long side of a rendered PDF page
MAX_CODES = 50                                           # barcodes in one reply, at most
TEXT_MAX = 8192                                          # characters of `text` (and bytes of `bytes_b64`) per barcode
TIMEOUT_S = _int("BCDECODER_TIMEOUT_S", 60, 1, 600)      # wall clock and CPU seconds of one job
QUEUE = _int("BCDECODER_QUEUE", 4, 0, 64)                # jobs waiting behind the running one; one more: 503
RETRY_AFTER_S = 30                                       # the Retry-After of a 503

MEMORY_BYTES = _int("BCDECODER_MEMORY_MB", 1536, 64, 16384) * MIB   # RLIMIT_AS of the job's child process
NOFILE = 64                                              # RLIMIT_NOFILE of the child
FILE_BYTES = 128 * MIB                                   # RLIMIT_FSIZE of the child (rendered pages)
MAX_CONNECTIONS = 32                                     # requests being handled at once; beyond: 503 at once
SOCKET_TIMEOUT_S = 30                                    # a client that stops sending is dropped after it
# A body is received before its job waits for a slot, so a slow client holds one connection, never a job slot.
# The whole body must arrive within max(BODY_MIN_S, its bytes / BODY_RATE) seconds (32 MiB: 128 s), else 408.
BODY_MIN_S = _int("BCDECODER_BODY_MIN_S", 30, 1, 600)
BODY_RATE = 256 * 1024                                   # bytes per second, the slowest body accepted beyond BODY_MIN_S
# Bytes of bodies on TMP_DIR at once (being received, waiting or decoding): the bound of 1 + QUEUE whole bodies that
# held before. A body that would go over it: 503 busy. Counted as the bytes arrive, so slow senders hold none
RECEIVE_BYTES = (1 + QUEUE) * MAX_BYTES

TMP_DIR = os.environ.get("BCDECODER_TMP") or "/tmp"      # a tmpfs in the container: /tmp/<random>/in per job


def limits():
    """The limits block of GET /health."""
    return {"max_bytes": MAX_BYTES, "max_pixels": MAX_PIXELS, "max_pages": MAX_PAGES,
            "default_pages": DEFAULT_PAGES, "pdf_side": PDF_SIDE, "max_codes": MAX_CODES, "text_max": TEXT_MAX,
            "timeout_s": TIMEOUT_S, "queue": QUEUE}
