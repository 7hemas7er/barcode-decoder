"""engine: the barcodes of a photo, a screenshot or a PDF. From boarding-pass-tracker's tracker/barcodes.py (the
decode and the PDF render), with the zbar fallback removed: zxing-cpp is the only decoder (Aztec, PDF417, QR and
every other format it reads). A PDF goes through pdftoppm: the first pages at PDF_SIDE pixels on the long side.

Two layers:
- the helpers the worker uses: sniff(), header_size(), open_image(), render_pdf(), results();
- read_barcodes(path, max_pixels): today's function, kept with its behaviour (texts stripped, cut at 500 characters,
  no duplicates, at most MAX_CODES; ValueError beyond max_pixels; [] on any other error), for the tests moved from
  tracker/test_barcodes.py.

A pure module: no network, no state but the cached decoder lookup. The only external command is pdftoppm, run only
on the file the worker hands over. A photo's size is read from its header before anything is decoded: a 2.5 MB JPEG
can ask for more than a gigabyte.
"""
import datetime, os, re, subprocess, tempfile

from PIL import Image

from . import isobmff

MAX_CODES = 20              # read_barcodes only: the API's cap is settings.MAX_CODES
PDF_SIDE = 2400             # pixels on the long side of a rendered PDF page
PDF_PAGES = 3
PDF_TIMEOUT = 60            # seconds: a hostile PDF must not keep the single worker busy
MAX_PIXELS = 50_000_000     # the iPhone's 48 Mpx photo passes
TMP_PREFIX = "bcdecoder-pages-"
NO_DECODER = "zxing-cpp is not installed: barcodes in photos and PDFs cannot be read"
_decoder = None             # None: not looked up yet; "": none available (logged only once)

# Pillow plugins per type: an image is opened only by the plugin of the type its magic bytes say
PIL_FORMATS = {"jpeg": ("JPEG",), "png": ("PNG",), "webp": ("WEBP",), "heic": ("HEIF",)}
_PAGE = re.compile(r"-(\d+)\.png$")


def _log(*a):
    print(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "barcodes:", *a, flush=True)


def sniff(head):
    """The type from the first bytes (at least 12), never from a name or a Content-Type: "jpeg", "png", "webp",
    "pdf", "heic", or None."""
    head = bytes(head[:16])
    if head[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head[:5] == b"%PDF-":
        return "pdf"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if isobmff.is_heif(head):
        return "heic"
    return None


def sniff_file(path):
    with open(path, "rb") as f:
        return sniff(f.read(16))


_heif_registered = False


def heif_available():
    """True when the HEIF plugin (pi-heif: libheif with the libde265 decoder only) can be loaded."""
    try:
        _register_heif()
        return True
    except Exception:
        return False


def _register_heif():
    global _heif_registered
    if not _heif_registered:
        import pi_heif
        pi_heif.register_heif_opener()
        _heif_registered = True


def open_image(path, kind):
    """Image.open restricted to the plugin of `kind` (PIL's bomb check stays on)."""
    if kind == "heic":
        _register_heif()
    return Image.open(path, formats=PIL_FORMATS[kind])


def header_size(path, kind):
    """(width, height) from the header, before anything is decoded. PIL's own bomb check is off for this read only,
    so a huge header gives its real size instead of an error. For HEIC: the largest `ispe` (the bounded reader of
    isobmff.py), the primary image's when it is the largest; libheif's header parse when the reader gives up."""
    if kind == "heic":
        with open(path, "rb") as f:
            primary, largest = isobmff.heif_sizes(f.read())
        if largest is not None:
            if primary is not None and primary[0] * primary[1] >= largest[0] * largest[1]:
                return primary
            return largest
    saved = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = None
    try:
        with open_image(path, kind) as im:
            return im.size
    finally:
        Image.MAX_IMAGE_PIXELS = saved


def render_pdf(path, out_dir, pages, side, timeout):
    """[(page number, png path)] of the first `pages` pages, `side` pixels on the long side. pdftoppm's errors raise
    (CalledProcessError, TimeoutExpired)."""
    # -scale-to and not -r: an A4 page stays at ~200 dpi, but with -r the giant page of a hostile PDF (up to 200
    # inches) would become a gigabyte image, and out of memory the machine freezes
    subprocess.run(["pdftoppm", "-scale-to", str(side), "-png", "-l", str(pages), os.path.abspath(path),
                    os.path.join(out_dir, "p")], capture_output=True, timeout=timeout, check=True,
                   stdin=subprocess.DEVNULL)
    out = []
    for name in sorted(os.listdir(out_dir)):
        m = _PAGE.search(name)
        if m:
            out.append((int(m.group(1)), os.path.join(out_dir, name)))
    return out


def parse_formats(value):
    """None (all formats) for None, else zxing-cpp's BarcodeFormats for a comma-separated list of its names, any
    case ("Aztec,PDF417,QRCode"). ValueError for an empty item, an unknown name or NONE."""
    if value is None:
        return None
    import zxingcpp
    names = [n.strip() for n in value.split(",")]
    if not names or len(value) > 512 or any(not n for n in names):
        raise ValueError("empty format name")
    for n in names:
        if zxingcpp.barcode_format_from_str(n) == zxingcpp.BarcodeFormat.NONE:     # unknown: ValueError
            raise ValueError("NONE")
    return zxingcpp.barcode_formats_from_str(",".join(names))


def results(im, formats=None):
    """zxing-cpp's barcodes of a PIL image, with its defaults (all formats, any direction; no EXIF transpose)."""
    import zxingcpp
    if formats is None:
        return zxingcpp.read_barcodes(im)
    return zxingcpp.read_barcodes(im, formats=formats)


# ---------------------------------------------------------------- today's read_barcodes (tracker/barcodes.py)
def _find_decoder():
    global _decoder
    if _decoder is None:
        try:
            import zxingcpp  # noqa: F401
            _decoder = "zxingcpp"
        except Exception:
            _decoder = ""
        if not _decoder:
            _log(NO_DECODER)
    return _decoder


def _decode(decoder, image):
    kind = sniff_file(image) or "png"
    with open_image(image, kind) as im:
        return [r.text for r in results(im)]


def _photo_size(path):
    """(width, height) from the header, or None for a PDF. Image.open raises DecompressionBombError beyond twice
    Image.MAX_IMAGE_PIXELS, also before decoding."""
    kind = sniff_file(path)
    if kind == "pdf":
        return None
    if kind is None:
        raise ValueError("unsupported type")
    if kind == "heic":                           # the header reader first, then PIL's own limit, as Image.open does
        w, h = header_size(path, kind)
        limit = Image.MAX_IMAGE_PIXELS
        if limit and w * h > 2 * limit:
            raise Image.DecompressionBombError(f"Image size ({w * h} pixels) exceeds limit of {2 * limit} pixels, "
                                               "could be decompression bomb DOS attack.")
        return w, h
    with open_image(path, kind) as im:
        return im.size


def read_barcodes(path, max_pixels=MAX_PIXELS):
    """Texts of the barcodes in the photo or in the first PDF_PAGES pages of the PDF, at most MAX_CODES, without
    duplicates. ValueError if the photo has more than `max_pixels` pixels (from the header, before decoding: also
    without a decoder), PIL's DecompressionBombError beyond its own limit; [] without a decoder, on a timeout or on
    any other error."""
    path = os.path.abspath(path)                 # starts with "/": cannot be taken for an option
    try:
        size = _photo_size(path)
    except Image.DecompressionBombError:
        raise
    except Exception as e:
        _log(f"barcodes not read ({type(e).__name__})")
        return []
    if size and size[0] * size[1] > max_pixels:
        raise ValueError(f"immagine di {size[0]}x{size[1]} pixel: troppo grande")
    try:
        decoder = _find_decoder()
        if not decoder:
            return []
        with tempfile.TemporaryDirectory(prefix=TMP_PREFIX) as td:
            images = [path]
            if size is None:
                images = [p for _, p in render_pdf(path, td, PDF_PAGES, PDF_SIDE, PDF_TIMEOUT)]
            out = []
            for im in images:
                for s in _decode(decoder, im):
                    s = s.strip()[:500]
                    if s and s not in out:
                        out.append(s)
            return out[:MAX_CODES]
    except Exception as e:
        _log(f"barcodes not read ({type(e).__name__})")
        return []
