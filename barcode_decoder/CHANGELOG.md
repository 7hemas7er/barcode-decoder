# Changelog

## 1.0.0

First release.

- HTTP API v1: `GET /health` (with `?selftest=1`) and `POST /v1/decode` (the raw file in, the barcodes out).
- Reads JPEG, PNG, WebP, PDF (the first pages, rendered by pdftoppm) and HEIC/HEIF (decode only, through libheif).
- zxing-cpp 3.1.1: every format it reads (Aztec, PDF417, QR Code, Data Matrix, the linear codes, ...).
- The pixel size is read from the header and refused beyond 50 megapixels before anything is decoded.
- One job at a time, in a child process with memory, CPU and time limits; 4 more wait; nothing is kept.
- A file is received whole before it waits for a job, within 30 s or its size at 256 KiB/s (408
  `body_timeout` after that), so slow clients hold no job; the files in `/tmp` at once are bounded (503 `busy`).
- Runs as UID 10001 and writes only to `/tmp`; read-only file system with the `docker run` line (an app's root
  file system is not read-only); no host port as a Home Assistant app.
