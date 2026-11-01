# Barcode Decoder

Reads the barcodes of an image or a PDF and returns their texts. It knows nothing about what the codes mean: a
boarding pass, a payment slip or a parcel label are all the same to it. It keeps nothing: each file lives in a
temporary folder in memory (`/tmp`) for the time of its request, and the log never contains a decoded text.

Accepted files, recognised by their content (never by name or `Content-Type`): **JPEG, PNG, WebP, HEIC/HEIF and
PDF** (the first 3 pages by default, up to 10). Formats: everything zxing-cpp 3.1.1 reads (Aztec, PDF417, QR Code,
Data Matrix, MaxiCode, the linear codes, ...).

## Installation

**As a Home Assistant app** (Home Assistant OS or Supervised):

- local app: copy the `barcode_decoder` folder into the `/addons` share (with the Samba or the Terminal & SSH app),
  then Settings > Apps > App store > ⋮ > Check for updates, open **Barcode Decoder**, Install, Start. The
  Supervisor builds the image on your device (amd64 or aarch64);
- from a repository: add the repository URL under Settings > Apps > App store > ⋮ > Repositories.

The app has **no host port and nothing to configure**. Check it from the Terminal & SSH app:

```sh
curl -s 'http://local-barcode-decoder:8099/health?selftest=1'
```

The answer has `"ok": true` and `"selftest": "ok"`.

**The address for Home Assistant** is the app's host name on the internal network: the app ID with `_` replaced by
`-`, and port 8099.

| Installed as | Address |
|---|---|
| local app (`/addons`) | `http://local-barcode-decoder:8099` |
| from a repository | `http://<repository hash>-barcode-decoder:8099` (the hash is in the app's URL in the app store) |

**With plain Docker** (Home Assistant Container, or any host):

```sh
docker run -d --name barcode-decoder --restart unless-stopped --read-only --tmpfs /tmp:size=256m \
  --memory 1500m --pids-limit 64 --cap-drop ALL --security-opt no-new-privileges \
  -p 127.0.0.1:8099:8099 ghcr.io/7hemas7er/barcode-decoder:1.0.0
```

- Home Assistant with `network_mode: host`: `http://127.0.0.1:8099`.
- Home Assistant on a user network: put both containers on it, leave out `-p`, and use
  `http://barcode-decoder:8099`.

Never publish the port on your LAN or on the internet: the decoder has no authentication (it holds no data, and
the network keeps it private). To build the image yourself: `docker build -t barcode-decoder barcode_decoder/`.

## API v1

### `GET /health`

```json
{"ok": true, "api": 1, "name": "barcode-decoder", "version": "1.0.0",
 "engine": {"name": "zxing-cpp", "version": "3.1.1"}, "pdf": {"renderer": "pdftoppm", "version": "25.03.0"},
 "heif": {"library": "libheif", "version": "1.23.0", "binding": "pi-heif", "binding_version": "1.4.0",
          "decoders": ["libde265"]},
 "types": ["jpeg", "png", "webp", "pdf", "heic"],
 "limits": {"max_bytes": 33554432, "max_pixels": 50000000, "max_pages": 10, "default_pages": 3,
            "pdf_side": 2400, "max_codes": 50, "text_max": 8192, "timeout_s": 60, "queue": 4}}
```

`GET /health?selftest=1` also decodes a fictional Aztec code shipped with the app, as PNG, as a PDF page rendered
by pdftoppm and as HEIC, and adds `"selftest": "ok"` (or `"failed"`) and `"selftest_checks"`.

### `POST /v1/decode`

The body is the raw file (not a form), with a `Content-Length`; send it as `application/octet-stream`.

| Query parameter | Default | Meaning |
|---|---|---|
| `formats` | all | zxing-cpp format names, comma-separated, any case (`Aztec,PDF417,QRCode`) |
| `pages` | 3 | PDF pages to read, 1 to 10 |
| `max_codes` | 50 | the most barcodes in the answer, 1 to 50 |
| `pixel_limit` | 50000000 | refuse images with more pixels (at most 50,000,000) |

The answer is 200 also when no code is found:

```json
{"api": 1, "type": "jpeg", "width": 3024, "height": 4032, "pages": null, "truncated": false, "elapsed_ms": 412,
 "barcodes": [{"page": null, "format": "Aztec", "text": "M1ROSSI/MARIO ...", "bytes_b64": "TTFS...",
               "content_type": "Text", "position": "120x80 900x82 898x860 118x858", "orientation": 0,
               "symbology_identifier": "]z0", "valid": true}]}
```

- `type` is `jpeg`, `png`, `webp`, `heic` or `pdf`. For a PDF, `width` and `height` are `null`, `pages` is the
  number of pages rendered and each barcode has its `page` (1 = the first).
- The barcodes come page by page, then in zxing-cpp's order. There is **no de-duplication**: the same code twice
  is listed twice.
- `text` and `bytes_b64` stop at 8192 characters/bytes; `truncated` is true when something was cut, or when there
  were more than `max_codes` barcodes.
- The decode uses zxing-cpp's defaults: any direction, inverted codes too. The EXIF orientation of a JPEG is not
  applied (not needed: the codes are found in any direction). HEIC rotations and mirrors (`irot`, `imir`) are
  applied by libheif.
- A PDF page is rendered at 2400 pixels on its long side (`pdftoppm -scale-to 2400`).

Errors are `{"error": {"code": "...", "message": "..."}}`:

| HTTP | `code` | When |
|---|---|---|
| 400 | `empty_body` | no `Content-Length`, or 0 |
| 400 | `bad_param` | an unknown, repeated or bad query parameter |
| 408 | `body_timeout` | the body did not arrive in time: it has 30 seconds, or more for a big file (its size at 256 KiB/s: 128 s for 32 MiB) |
| 413 | `too_large` | the `Content-Length` is over `max_bytes` (32 MiB): refused before the body is read |
| 415 | `unsupported_type` | not JPEG, PNG, WebP, HEIC/HEIF or PDF (for example AVIF, GIF, TIFF, SVG, XML) |
| 422 | `too_many_pixels` (`width`, `height`) | the size in the image's header is over the limit; nothing was decoded |
| 422 | `unreadable` | the image or the PDF cannot be opened, decoded or rendered |
| 503 | `busy` (+ `Retry-After: 30`) | one job running and 4 waiting, or the files being received already fill the room kept for them in `/tmp` (5 × 32 MiB) |
| 504 | `timeout` | the job ran past 60 seconds and was stopped |
| 500 | `internal` | the job's process died (for example out of memory) |

Any other request gets a JSON error of the same shape too: a wrong path is 404 `not_found`, a wrong method is 405
`method_not_allowed` (with an `Allow` header), an unknown HTTP method is 501 `not_implemented`, a malformed
request line or header is 400 `bad_request`, a request line that is too long is 414 `uri_too_long`, too many or too
big headers are 431 `headers_too_large`, and an
unsupported HTTP version is 505 `http_version`. A client that stays silent for 30 seconds while it sends the request
line or the headers is dropped without an answer. More than 32 connections at once get 503 `busy` at once. The only
routes are `GET /health` and `POST /v1/decode`.

## Using it with Home Assistant

Put the address of the table above in the **Indirizzo del decodificatore** field of the boarding-pass-tracker
integration (setup or options): it calls `GET /health` and needs `"api": 1` in the answer. The integration sends each
photo or PDF to `POST /v1/decode?pages=3&max_codes=50`, and keeps nothing of the answer but the barcode texts.

- On Home Assistant OS the integration suggests the right host name by itself.
- With Docker, **the host name must be the one Home Assistant can resolve**: `127.0.0.1` only when Home Assistant
  runs with `network_mode: host`, or the container's name when both containers share a Docker network.
- HEIC photos (the iPhone's default) are read here, so the iOS Shortcut needs no conversion step. HEIC needs the
  image: a source checkout without pi-heif answers 415 for it.

## How it protects itself

- One job at a time, in a separate process limited to 1.5 GiB of memory, 60 seconds of CPU and 64 open files, and
  stopped after 60 seconds; 4 more requests wait, the next ones get 503.
- A file is received whole before its request waits for a job, so a client that sends slowly takes one of the 32
  connections, never a job. The whole file must arrive within 30 seconds, or its size at 256 KiB/s if that is
  longer (408 after that); a client that sends nothing for 30 seconds is dropped. The files in `/tmp` at once
  (received, waiting or decoding) are counted as their bytes arrive and never exceed 5 × 32 MiB (503 beyond).
- The type comes from the first bytes only; Pillow opens a file only with the plugin of that type.
- The pixel size is read from the header before decoding (a small file can describe a gigantic image). For HEIC it
  is read from the `ispe` property by a small, bounded reader, before libheif is involved.
- HEIC is decode-only: there is no HEIC encoder in the image.
- The process runs as user 10001 and writes only to `/tmp`, a memory file system. With the `docker run` line above
  the whole file system is also read-only (`--read-only`); as a Home Assistant app the root file system is not
  made read-only, so the limit there is what the decoder does, not what the container allows.
- The request line and the headers have no overall time limit: only each 30-second silence is cut. A slow client
  that keeps sending a byte now and then can therefore still hold some of the 32 connections (never a job), so the
  private network is the protection.

## Log

One line per decode or self-test, for example:

```
2026-10-31 22:46:27 decode type=heic bytes=9200 pixels=800x600 pages=- codes=1 ms=115 status=200 code=ok
```

Never the decoded text, never the file. Plain `GET /health` calls (the watchdog's) are not logged.
