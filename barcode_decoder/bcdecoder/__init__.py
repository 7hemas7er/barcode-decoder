"""barcode-decoder: a stateless HTTP service that reads the barcodes of an image or a PDF.

Image or PDF in, barcode texts out. Nothing is kept between requests: no database, no users, no tokens, no outbound
connection. Modules:

- settings: the limits (not config.py: the Supervisor reads every config.* of an app repository as an app);
- engine: the decode and the PDF render, from boarding-pass-tracker's tracker/barcodes.py (the zbar fallback is gone);
- isobmff: a bounded reader of the pixel size of a HEIF/HEIC file, from its header;
- worker: one job, run in a child process with resource limits;
- server: the HTTP API v1 (GET /health, POST /v1/decode).
"""
__version__ = "1.0.0"
API = 1
NAME = "barcode-decoder"
