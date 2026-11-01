"""worker: one job, in a child process of the server.

  python -I -B -c "<bootstrap>" <package parent> <job dir>     (see server.WORKER_BOOT)

The job dir (a fresh /tmp/<random>, removed by the server) holds `in` (the file), `job.json` (the type and the
parameters) and, at the end, `out.json` (the reply). The child first lowers its own limits (address space, CPU
seconds, open files, file size, no core dump), so a crash or a runaway in Pillow, libheif, poppler or zxing-cpp
ends here and not in the server. Nothing is printed: the decoded texts go only to out.json.

Order for an image: the pixel size from the header, refused beyond the limit before anything is decoded; then the
decode. For a PDF: pdftoppm renders the first pages at PDF_SIDE on the long side (so a page cannot be huge), then
each page is decoded, page 1 first.
"""
import base64, hashlib, json, os, resource, subprocess, sys, time

from PIL import Image

from . import engine

# sha256 of the text of the Aztec code in selftest.png / selftest.heic: a fictional boarding pass, the default BCBP
# of boarding-pass-tracker's tests (the text itself is not needed here)
SELFTEST_SHA256 = "2d505218155262e0073804e46e32f80fc239dd13cc8dba765cac2d59eb1d6be6"


class JobError(Exception):
    def __init__(self, status, code, message, **extra):
        super().__init__(code)
        self.status, self.code, self.message, self.extra = status, code, message, extra


def set_limits(job):
    t = int(job["timeout_s"])
    soft, hard = resource.getrlimit(resource.RLIMIT_CPU)
    cpu_hard = t + 2 if hard == resource.RLIM_INFINITY else min(t + 2, hard)
    resource.setrlimit(resource.RLIMIT_CPU, (min(t, cpu_hard), cpu_hard))     # SIGXCPU at t seconds
    for kind, key in ((resource.RLIMIT_AS, "memory_bytes"), (resource.RLIMIT_NOFILE, "nofile"),
                      (resource.RLIMIT_FSIZE, "file_bytes")):
        soft, hard = resource.getrlimit(kind)
        value = int(job[key]) if hard == resource.RLIM_INFINITY else min(int(job[key]), hard)
        resource.setrlimit(kind, (value, value))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def _name(value):
    return getattr(value, "name", None) or str(value)


def barcode(r, page, text_max):
    """The reply object of one zxing-cpp result, and whether its text or bytes were cut."""
    text = r.text or ""
    raw = bytes(r.bytes or b"")
    cut = len(text) > text_max or len(raw) > text_max
    return {"page": page, "format": _name(r.format), "text": text[:text_max],
            "bytes_b64": base64.b64encode(raw[:text_max]).decode("ascii"),
            "content_type": _name(r.content_type), "position": str(r.position),
            "orientation": int(r.orientation), "symbology_identifier": r.symbology_identifier,
            "valid": bool(r.valid)}, cut


def _memory_probe(im):
    """zxing-cpp turns every failure to read a PIL image's pixels into TypeError("Invalid input ..."), a MemoryError
    included. Copy the pixels as it would: a MemoryError here means the job ran out of memory (500), not that the
    file is unreadable."""
    probe = im if im.mode in ("L", "RGB", "RGBA") else im.convert("L")
    probe.tobytes()


def decode(job_dir, job, started):
    kind = job["type"]
    path = os.path.join(job_dir, "in")
    try:
        formats = engine.parse_formats(job.get("formats"))
    except ValueError:
        raise JobError(400, "bad_param", "formats: not a list of zxing-cpp format names") from None
    if kind == "pdf":
        pages_dir = os.path.join(job_dir, "pages")
        os.mkdir(pages_dir, 0o700)
        remaining = job["timeout_s"] - (time.monotonic() - started) - 1
        try:
            pages = engine.render_pdf(path, pages_dir, job["pages"], job["pdf_side"], timeout=max(1.0, remaining))
        except subprocess.TimeoutExpired:
            raise JobError(504, "timeout", "the PDF took too long to render") from None
        except (subprocess.CalledProcessError, OSError):
            raise JobError(422, "unreadable", "the PDF cannot be rendered") from None
        images = [(n, p, "png") for n, p in pages]
        width = height = None
        npages = len(pages)
    else:
        try:
            width, height = engine.header_size(path, kind)
        except MemoryError:
            raise
        except Exception:
            raise JobError(422, "unreadable", "the image cannot be opened") from None
        if width * height > job["pixel_limit"]:
            raise JobError(422, "too_many_pixels", f"the image has more than {job['pixel_limit']} pixels",
                           width=width, height=height)
        images = [(None, path, kind)]
        npages = None
    barcodes, truncated = [], False
    for page, image, image_kind in images:
        try:
            with engine.open_image(image, image_kind) as im:
                if page is None:
                    width, height = im.size
                im.load()                           # zxing-cpp loads it too; here a MemoryError stays one
                try:
                    found = engine.results(im, formats)
                except TypeError:
                    _memory_probe(im)
                    raise
        except Image.DecompressionBombError:
            raise JobError(422, "too_many_pixels", "the image is too large", width=width, height=height) from None
        except MemoryError:
            raise
        except Exception:
            raise JobError(422, "unreadable", "the image cannot be decoded") from None
        for r in found:
            if len(barcodes) >= job["max_codes"]:
                truncated = True
                break
            item, cut = barcode(r, page, job["text_max"])
            truncated = truncated or cut
            barcodes.append(item)
    return {"api": 1, "type": kind, "width": width, "height": height, "pages": npages, "truncated": truncated,
            "elapsed_ms": int((time.monotonic() - started) * 1000), "barcodes": barcodes}


def _selftest_ok(found):
    return any(_name(r.format) == "Aztec" and hashlib.sha256((r.text or "").encode()).hexdigest() == SELFTEST_SHA256
               for r in found)


def selftest(job_dir, job):
    """Decode the shipped fictional Aztec code as PNG, as a one-page PDF rendered by pdftoppm, and as HEIC."""
    src = job["selftest_dir"]
    png = os.path.join(src, "selftest.png")
    checks = {}
    try:
        with engine.open_image(png, "png") as im:
            checks["png"] = "ok" if _selftest_ok(engine.results(im)) else "failed"
    except Exception:
        checks["png"] = "failed"
    if job.get("pdf"):
        try:
            pdf = os.path.join(job_dir, "selftest.pdf")
            with Image.open(png, formats=("PNG",)) as code:
                page = Image.new("L", (1240, 1754), 255)            # A4 at 150 dpi, the code in a corner
                page.paste(code.convert("L"), (100, 100))
                page.save(pdf, "PDF", resolution=150)
            pages_dir = os.path.join(job_dir, "pages")
            os.mkdir(pages_dir, 0o700)
            ok = False
            for _, image in engine.render_pdf(pdf, pages_dir, 1, job["pdf_side"], timeout=job["timeout_s"] / 2):
                with engine.open_image(image, "png") as im:
                    ok = ok or _selftest_ok(engine.results(im))
            checks["pdf"] = "ok" if ok else "failed"
        except Exception:
            checks["pdf"] = "failed"
    if job.get("heic"):
        try:
            with engine.open_image(os.path.join(src, "selftest.heic"), "heic") as im:
                checks["heic"] = "ok" if _selftest_ok(engine.results(im)) else "failed"
        except Exception:
            checks["heic"] = "failed"
    return {"checks": checks}


def _write(job_dir, result):
    tmp = os.path.join(job_dir, "out.json.part")
    with open(tmp, "w") as f:
        json.dump(result, f)
    os.replace(tmp, os.path.join(job_dir, "out.json"))


def main(job_dir):
    started = time.monotonic()
    with open(os.path.join(job_dir, "job.json")) as f:
        job = json.load(f)
    set_limits(job)
    try:
        if job.get("mode") == "selftest":
            result = {"status": 200, "body": selftest(job_dir, job)}
        else:
            result = {"status": 200, "body": decode(job_dir, job, started)}
    except JobError as e:
        result = {"status": e.status, "error": {"code": e.code, "message": e.message, **e.extra}}
    except BaseException as e:                       # MemoryError and bugs: the server answers 500 internal
        result = {"status": 500, "error": {"code": "internal", "message": "the decoder failed"},
                  "exc": type(e).__name__}
    _write(job_dir, result)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
