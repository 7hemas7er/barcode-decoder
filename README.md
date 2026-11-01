# barcode-decoder

A small, stateless HTTP service that reads the barcodes of images and PDFs: the file goes in, the texts of the
barcodes come out. It is a Home Assistant app repository (formerly "add-on"), and the same image runs with plain
Docker.

- **Input:** JPEG, PNG, WebP, HEIC/HEIF, PDF (recognised by content).
- **Engine:** [zxing-cpp](https://github.com/zxing-cpp/zxing-cpp) 3.1.1, every format it reads; PDF pages rendered by
  poppler's `pdftoppm`; HEIC decoded by libheif (decode only).
- **Stateless:** no database, no users, no tokens, no outbound connection. Nothing is kept between requests and
  the log never holds a decoded text.
- **Contained:** one job at a time in a child process with memory, CPU and time limits; UID 10001; writes only to `/tmp`
  (read-only file system with the `docker run` line; not as an app); no host port as an app.

It was written for the Home Assistant integration
[boarding-pass-tracker](https://github.com/7hemas7er/boarding-pass-tracker) (the Aztec/PDF417 code of a boarding pass),
but the API is generic: any local service can send it a photo of a QR code or a PDF bill.

The user documentation (installation, the address to use, the API) is
[barcode_decoder/DOCS.md](barcode_decoder/DOCS.md).

## Repository layout

```
repository.yaml              the Home Assistant app repository
barcode_decoder/             the app folder = the Docker build context
  config.yaml                the app (the only config.* file in the repository: the Supervisor reads every one)
  Dockerfile                 python:3.13-slim-trixie pinned by digest + poppler-utils + the wheels
  requirements.txt           zxing-cpp, Pillow, pi-heif: wheels pinned with hashes (aarch64 and x86_64)
  DOCS.md README.md CHANGELOG.md icon.png logo.png
  selftest.png selftest.heic a fictional Aztec code for GET /health?selftest=1
  bcdecoder/                 the package
    server.py                HTTP API v1 (stdlib ThreadingHTTPServer)
    worker.py                one job in a child process with resource limits
    engine.py                decode + PDF render (from boarding-pass-tracker's tracker/barcodes.py)
    isobmff.py               the bounded HEIF header reader (ispe) for the pixel guard
    settings.py              the limits (not config.py, see above)
tests/                       T3: unit and HTTP tests run inside the image; tests/host: the container and the layout
THIRD_PARTY.md               every shipped component and its licence (GPL ones flagged)
```

## Tests

Docker is the only requirement; the suite runs inside the image it tests (Python 3.13, the pinned wheels, poppler):

```sh
sh tests/run_t3.sh                  # build local/barcode-decoder:1.0.0, run tests/ in it, then tests/host
BUILD=0 sh tests/run_t3.sh          # without building
```

- `tests/test_barcodes.py`: the 8 tests moved from boarding-pass-tracker's `tracker/test_barcodes.py`, adapted.
- `tests/test_api.py`: the HTTP API (formats and angles, EXIF, PDF pages, the pixel guard, unreadable and
  unsupported files, 413/400, timeouts with the job's processes killed, one 503 out of six parallel requests,
  slow senders that hold no job slot, the 408 of a body slower than its deadline, the bound of the files in
  `/tmp`, a child out of memory, no decoded text in the log).
- `tests/test_heic.py`: HEIC by magic bytes, the bounded header reader against hostile and damaged headers, the
  pixel guard before libheif decodes anything, non-HEIF `ftyp` brands refused.
- `tests/test_worker.py`: the worker and the server's helpers in process.
- `tests/host/`: the documented `docker run` (read-only, UID 10001, no capabilities, tmpfs `/tmp`), the self-test,
  no HEIC encoder in the image, a quick stop as PID 1; the repository layout and packaging rules (needs PyYAML).

The HEIC fixtures are made by `tests/fixtures/make_fixtures.py` in a throwaway container that has an encoder (see
its docstring); the image never contains one.

To run the server from a checkout (Python ≥ 3.12 with the requirements and `pdftoppm` installed):

```sh
cd barcode_decoder && python3 -B -m bcdecoder.server          # BCDECODER_PORT=0 picks a free port
```

## Publishing

A release (a `v<version>` tag) builds `ghcr.io/7hemas7er/barcode-decoder:<version>` for linux/amd64 and
linux/arm64 (`.github/workflows/release.yml`); the app keeps building from the Dockerfile on the device until an
`image:` key is added to `config.yaml`.

## Licence

MIT ([LICENSE](LICENSE)). The image's third-party components keep their licences: [THIRD_PARTY.md](THIRD_PARTY.md).
