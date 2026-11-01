"""server: the HTTP API v1 of the decoder (stdlib only).

  python -I -B -m bcdecoder.server          listens on BCDECODER_HOST:BCDECODER_PORT (default 0.0.0.0:8099)

GET /health                -> 200 {"ok", "api": 1, "name", "version", "engine", "pdf", "heif", "types", "limits"}
GET /health?selftest=1     -> the same + "selftest": "ok"|"failed" (+ "selftest_checks" per type)
POST /v1/decode            -> the body is the raw file; Content-Length is required. Query: formats, pages,
                              max_codes, pixel_limit. 200 {"api": 1, "type", "width", "height", "pages",
                              "truncated", "elapsed_ms", "barcodes": [...]}, also when no code is found.
Errors: {"error": {"code", "message", ...}} with 400 empty_body/bad_param, 408 body_timeout, 413 too_large,
415 unsupported_type, 422 too_many_pixels/unreadable, 503 busy (+ Retry-After), 504 timeout, 500 internal.

One job runs at a time, in a child process (worker.py) with its own limits and a wall-clock kill; QUEUE more wait;
one more gets 503 at once. The body is received into a fresh /tmp/<random>/in BEFORE the job takes a slot, so a
client that sends slowly holds one of MAX_CONNECTIONS connections and never a job slot; the whole body has
max(BODY_MIN_S, bytes / BODY_RATE) seconds (408 body_timeout after that), and the bodies on /tmp at once are
counted as they arrive and bounded by RECEIVE_BYTES (503 busy beyond). The file is removed when the request ends.
Nothing is kept between requests. The log has one line per decode or selftest: type, bytes, pixels, pages, codes, ms, status and
error code, never a decoded text or byte (a boarding pass's barcode holds a full name and a booking code).
"""
import datetime, importlib.metadata, json, os, re, shutil, signal, subprocess, sys, tempfile, threading, time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import API, NAME, __version__, engine, settings

PKG_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SELFTEST_DIR = PKG_PARENT                          # selftest.png and selftest.heic sit beside the package
# The child: -I (isolated: no PYTHON* variables, no user site, no current directory on sys.path) and -B; the
# package's parent goes first on sys.path explicitly, so the same command works from the image and from a checkout
WORKER_BOOT = ("import sys; sys.path.insert(0, sys.argv[1]); from bcdecoder import worker; "
               "sys.exit(worker.main(sys.argv[2]))")
QUERY_KEYS = ("formats", "pages", "max_codes", "pixel_limit")
OUT_MAX = 8 << 20                                  # out.json larger than this is a broken child
_DIGITS = re.compile(r"^[0-9]{1,10}$")


def log(*a):
    print(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


class ApiError(Exception):
    def __init__(self, status, code, message, headers=(), **extra):
        super().__init__(code)
        self.status, self.code, self.message, self.headers, self.extra = status, code, message, headers, extra

    def body(self):
        return {"error": {"code": self.code, "message": self.message, **self.extra}}


class ClientGone(Exception):
    pass


def busy_error():
    return ApiError(503, "busy", "the decoder is busy: retry later", headers=(("Retry-After",
                                                                               str(settings.RETRY_AFTER_S)),))


def body_seconds(n):
    """The seconds a body of n bytes has to arrive whole: max(BODY_MIN_S, n / BODY_RATE)."""
    return max(settings.BODY_MIN_S, -(-n // settings.BODY_RATE))


class BodyLate(Exception):
    """The body did not arrive by its deadline."""


class Budget:
    """The bytes of request bodies on TMP_DIR at once (being received, waiting for a job slot, or in the running
    job). Taken as the bytes arrive, so a slow sender holds only what it really sent; given back when the request's
    folder is removed."""

    def __init__(self, total):
        self.total, self.used, self._lock = total, 0, threading.Lock()

    def take(self, n):
        with self._lock:
            if self.used + n > self.total:
                return False
            self.used += n
            return True

    def give(self, n):
        with self._lock:
            self.used -= n


def parse_query(query):
    """The decode parameters from the query string; ApiError 400 bad_param for an unknown, repeated or bad one."""
    try:
        pairs = urllib.parse.parse_qsl(query, keep_blank_values=True, max_num_fields=len(QUERY_KEYS))
    except ValueError:
        raise ApiError(400, "bad_param", "too many query parameters") from None
    seen = {}
    for key, value in pairs:
        if key not in QUERY_KEYS:
            raise ApiError(400, "bad_param", f"unknown query parameter: {key[:40]}")
        if key in seen:
            raise ApiError(400, "bad_param", f"repeated query parameter: {key}")
        seen[key] = value

    def number(key, low, high, default):
        if key not in seen:
            return default
        value = seen[key]
        if not _DIGITS.match(value) or not low <= int(value) <= high:
            raise ApiError(400, "bad_param", f"{key}: an integer from {low} to {high}")
        return int(value)

    params = {"pages": number("pages", 1, settings.MAX_PAGES, settings.DEFAULT_PAGES),
              "max_codes": number("max_codes", 1, settings.MAX_CODES, settings.MAX_CODES),
              "pixel_limit": number("pixel_limit", 1, settings.MAX_PIXELS, settings.MAX_PIXELS),
              "formats": seen.get("formats")}
    if params["formats"] is not None:
        try:
            engine.parse_formats(params["formats"])
        except ImportError:
            pass                                    # no zxing-cpp in this process: the worker answers
        except ValueError:
            raise ApiError(400, "bad_param", "formats: a comma-separated list of zxing-cpp format names") from None
    return params


def _kill_group(pgid):
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _reap_group(pgid, wait_s=1.0):
    """Reap what is left of the job's process group (a pdftoppm whose parent was killed). Such orphans are our
    children only when this server is PID 1 (a plain `docker run` without --init); elsewhere init reaps them."""
    deadline = time.monotonic() + wait_s
    while True:
        try:
            pid, _ = os.waitpid(-pgid, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            if time.monotonic() > deadline:
                return
            time.sleep(0.02)


def child_outcome(returncode, out, timed_out):
    """(status, payload, note) of a finished child: its out.json, or 504/500 when it has none."""
    if timed_out:
        return 504, {"error": {"code": "timeout", "message": f"the job ran past {settings.TIMEOUT_S} s"}}, "killed"
    if returncode == -signal.SIGXCPU:
        return 504, {"error": {"code": "timeout", "message": "the job used all its CPU time"}}, "SIGXCPU"
    if isinstance(out, dict) and isinstance(out.get("status"), int):
        status = out["status"]
        if status == 200 and isinstance(out.get("body"), dict):
            return 200, out["body"], ""
        if status != 200 and isinstance(out.get("error"), dict):
            return status, {"error": out["error"]}, str(out.get("exc") or "")[:60]
    if returncode is not None and returncode < 0:
        try:
            note = signal.Signals(-returncode).name
        except ValueError:
            note = f"signal {-returncode}"
    else:
        note = f"exit {returncode}"
    return 500, {"error": {"code": "internal", "message": "the decoder process died"}}, note


class Decoder:
    """What the request threads share: the job slots, the run lock, the versions."""

    def __init__(self):
        self.slots = threading.BoundedSemaphore(1 + settings.QUEUE)
        self.run_lock = threading.Lock()
        self.connections = threading.BoundedSemaphore(settings.MAX_CONNECTIONS)
        self.budget = Budget(settings.RECEIVE_BYTES)
        self.active = None
        self.env = dict(os.environ, MALLOC_ARENA_MAX="2")
        self.info = self._probe()
        self.types = ["jpeg", "png", "webp"]
        if self.info["pdf"]["version"]:
            self.types.append("pdf")
        if self.info["heif"]:
            self.types.append("heic")

    @staticmethod
    def _probe():
        zx = None
        try:
            import zxingcpp  # noqa: F401
            zx = importlib.metadata.version("zxing-cpp")
        except Exception:
            pass
        poppler = None
        try:
            r = subprocess.run(["pdftoppm", "-v"], capture_output=True, timeout=10, stdin=subprocess.DEVNULL)
            m = re.search(rb"version ([0-9][0-9.]*)", r.stderr + r.stdout)
            poppler = m.group(1).decode() if m else None
        except (OSError, subprocess.SubprocessError):
            pass
        heif = None
        try:
            import pi_heif
            decoders = pi_heif.libheif_info().get("decoders") or {}
            if decoders:
                heif = {"library": "libheif", "version": pi_heif.libheif_version(), "binding": "pi-heif",
                        "binding_version": pi_heif.__version__, "decoders": sorted(decoders)}
        except Exception:
            pass
        return {"engine": {"name": "zxing-cpp", "version": zx}, "pdf": {"renderer": "pdftoppm", "version": poppler},
                "heif": heif}

    def health(self):
        return {"ok": bool(self.info["engine"]["version"]), "api": API, "name": NAME, "version": __version__,
                "engine": self.info["engine"], "pdf": self.info["pdf"], "heif": self.info["heif"],
                "types": list(self.types), "limits": settings.limits()}

    def job(self, mode, **extra):
        return {"mode": mode, "timeout_s": settings.TIMEOUT_S, "memory_bytes": settings.MEMORY_BYTES,
                "nofile": settings.NOFILE, "file_bytes": settings.FILE_BYTES, "pdf_side": settings.PDF_SIDE,
                "text_max": settings.TEXT_MAX, **extra}

    def run(self, job_dir, job):
        """Run one job in a child; the caller holds run_lock. (status, payload, note)."""
        with open(os.path.join(job_dir, "job.json"), "w") as f:
            json.dump(job, f)
        proc = subprocess.Popen([sys.executable, "-I", "-B", "-c", WORKER_BOOT, PKG_PARENT, job_dir],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                cwd=job_dir, env=self.env, start_new_session=True, close_fds=True)
        self.active = proc
        timed_out = False
        try:
            proc.wait(timeout=settings.TIMEOUT_S + 1)
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            _kill_group(proc.pid)                   # the child and anything it started (pdftoppm)
            proc.wait()
            _reap_group(proc.pid)
            self.active = None
        out = None
        try:
            path = os.path.join(job_dir, "out.json")
            if os.path.getsize(path) <= OUT_MAX:
                with open(path) as f:
                    out = json.load(f)
        except (OSError, ValueError):
            pass
        return child_outcome(proc.returncode, out, timed_out)

    def stop(self):
        proc = self.active
        if proc is not None:
            _kill_group(proc.pid)


class Handler(BaseHTTPRequestHandler):
    server_version = f"{NAME}/{__version__}"
    sys_version = ""
    protocol_version = "HTTP/1.0"                   # one request per connection
    timeout = settings.SOCKET_TIMEOUT_S

    # ------------------------------------------------------------ plumbing
    def log_message(self, format, *args):           # noqa: A002 - the stdlib's name; our own line is in _log_job
        pass

    def version_string(self):
        return self.server_version

    def handle(self):
        decoder = self.server.decoder
        if not decoder.connections.acquire(blocking=False):
            body = json.dumps(busy_error().body()).encode()
            try:
                self.wfile.write(b"HTTP/1.0 503 Service Unavailable\r\nContent-Type: application/json\r\n"
                                 + f"Retry-After: {settings.RETRY_AFTER_S}\r\nContent-Length: {len(body)}\r\n"
                                   "Connection: close\r\n\r\n".encode() + body)
            except OSError:
                pass
            return
        try:
            super().handle()
        finally:
            decoder.connections.release()

    def _send(self, status, payload, headers=()):
        data = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def send_error(self, code, message=None, explain=None):
        """The stdlib's own errors (bad request line, headers too large, unknown method) as JSON."""
        self.close_connection = True
        names = {400: "bad_request", 404: "not_found", 405: "method_not_allowed", 408: "request_timeout",
                 414: "uri_too_long", 431: "headers_too_large", 501: "not_implemented", 505: "http_version"}
        short = self.responses.get(code, ("error",))[0]
        try:
            self._send(code, {"error": {"code": names.get(code, "error"), "message": message or short}})
        except OSError:
            pass

    def _content_length(self):
        raw = self.headers.get("Content-Length")
        if raw is None or raw.strip() in ("", "0"):
            raise ApiError(400, "empty_body", "the body is the file: send it with a Content-Length")
        raw = raw.strip()
        if not _DIGITS.match(raw.lstrip("0") or "0") or len(raw) > 12:
            raise ApiError(400, "bad_param", "Content-Length: not a number")
        if int(raw) == 0:
            raise ApiError(400, "empty_body", "the body is the file: send it with a Content-Length")
        return int(raw)

    def _start_body(self, n):
        """The deadline of a body of n bytes, from now: one for the whole body, not for each read, so a client that
        sends a byte now and then is cut off too."""
        self.body_s = body_seconds(n)
        self.body_until = time.monotonic() + self.body_s

    def _read1(self, want):
        """At most `want` bytes of the body with one read (b"" when the client closed). OSError (also a client that
        sends nothing for SOCKET_TIMEOUT_S): ClientGone. Past the body's deadline: BodyLate."""
        left = self.body_until - time.monotonic()
        if left <= 0:
            raise BodyLate()
        by_deadline = left < settings.SOCKET_TIMEOUT_S      # this read's timeout is the body's deadline
        self.connection.settimeout(max(0.01, min(settings.SOCKET_TIMEOUT_S, left)))
        try:
            return self.rfile.read1(want)
        except TimeoutError:
            raise (BodyLate() if by_deadline else ClientGone()) from None
        except OSError:
            raise ClientGone() from None
        finally:
            try:
                self.connection.settimeout(settings.SOCKET_TIMEOUT_S)
            except OSError:
                pass

    def _discard(self, n):
        """Read and drop n bytes, so the client gets the answer instead of a reset; gives up at the body's deadline
        or when the client stops."""
        left = n
        try:
            while left > 0:
                chunk = self._read1(min(left, 1 << 16))
                if not chunk:
                    break
                left -= len(chunk)
        except (ClientGone, BodyLate):
            self.close_connection = True

    def _receive(self, n, path, held):
        """The body into path (0600), each chunk counted in the budget before it is written; held[0] is what this
        request holds of the budget (the caller gives it back). A body that does not fit: the rest is read and
        dropped, then 503 busy."""
        budget = self.server.decoder.budget
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            left = n
            while left > 0:
                chunk = self._read1(min(left, 1 << 16))
                if not chunk:
                    raise ClientGone()
                left -= len(chunk)
                if not budget.take(len(chunk)):
                    self._discard(left)
                    raise busy_error()
                held[0] += len(chunk)
                f.write(chunk)

    def _split(self):
        url = urllib.parse.urlsplit(self.path)
        return url.path, url.query

    def _not_allowed(self, allow):
        self.close_connection = True
        self._send(405, {"error": {"code": "method_not_allowed", "message": f"use {allow}"}}, (("Allow", allow),))

    def _not_found(self):
        self.close_connection = True
        self._send(404, {"error": {"code": "not_found", "message": "GET /health or POST /v1/decode"}})

    def _drop_small_body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return
        if 0 < n <= (1 << 20):
            self._start_body(n)
            self._discard(n)

    # ------------------------------------------------------------ routes
    def do_GET(self):
        path, query = self._split()
        if path == "/health":
            self._health(query)
        elif path == "/v1/decode":
            self._not_allowed("POST")
        else:
            self._not_found()

    def do_POST(self):
        path, query = self._split()
        if path == "/v1/decode":
            self._decode(query)
            return
        self._drop_small_body()
        if path == "/health":
            self._not_allowed("GET")
        else:
            self._not_found()

    def _other(self):
        self._drop_small_body()
        path, _ = self._split()
        if path == "/health":
            self._not_allowed("GET")
        elif path == "/v1/decode":
            self._not_allowed("POST")
        else:
            self._not_found()

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = _other

    # ------------------------------------------------------------ health
    def _health(self, query):
        decoder = self.server.decoder
        body = decoder.health()
        try:
            wants = dict(urllib.parse.parse_qsl(query, keep_blank_values=True, max_num_fields=8)
                         if len(query) < 256 else [])
        except ValueError:
            wants = {}
        if wants.get("selftest") not in ("1", "true", "yes"):
            self._send(200, body)
            return
        started = time.monotonic()
        if not decoder.slots.acquire(blocking=False):
            e = busy_error()
            self._log_job("selftest", "-", 0, {}, 503, e.code, started)
            self._send(e.status, e.body(), e.headers)
            return
        try:
            job_dir = tempfile.mkdtemp(dir=settings.TMP_DIR)
            try:
                job = decoder.job("selftest", pdf="pdf" in decoder.types, heic="heic" in decoder.types,
                                  selftest_dir=SELFTEST_DIR)
                with decoder.run_lock:
                    status, payload, note = decoder.run(job_dir, job)
            finally:
                shutil.rmtree(job_dir, ignore_errors=True)
        finally:
            decoder.slots.release()
        checks = payload.get("checks") if status == 200 else None
        checks = checks if isinstance(checks, dict) else {}
        ok = checks.get("png") == "ok" and all(v == "ok" for v in checks.values())
        body["selftest"] = "ok" if ok else "failed"
        body["selftest_checks"] = checks
        self._log_job("selftest", "-", 0, {}, 200, body["selftest"], started, note)
        self._send(200, body)

    # ------------------------------------------------------------ decode
    def _decode(self, query):
        decoder = self.server.decoder
        started = time.monotonic()
        kind, size, status, code, note, reply = "-", 0, 500, "internal", "", {}
        held = [0]                                  # bytes of the budget this request holds
        try:
            size = self._content_length()
            if size > settings.MAX_BYTES:
                self.close_connection = True       # the body is not read
                raise ApiError(413, "too_large", f"the file is larger than {settings.MAX_BYTES} bytes",
                               max_bytes=settings.MAX_BYTES)
            self._start_body(size)
            try:
                params = parse_query(query)
            except ApiError:
                self._discard(size)
                raise
            job_dir = tempfile.mkdtemp(dir=settings.TMP_DIR)
            try:
                path = os.path.join(job_dir, "in")
                self._receive(size, path, held)     # first the whole body: a slow sender holds no job slot
                if not decoder.slots.acquire(blocking=False):
                    raise busy_error()
                try:
                    kind = engine.sniff_file(path) or "-"
                    if kind not in decoder.types:
                        raise ApiError(415, "unsupported_type",
                                       "the file is not one of: " + ", ".join(decoder.types))
                    job = decoder.job("decode", type=kind, **params)
                    with decoder.run_lock:
                        status, reply, note = decoder.run(job_dir, job)
                finally:
                    decoder.slots.release()
            finally:
                shutil.rmtree(job_dir, ignore_errors=True)
                decoder.budget.give(held[0])
            if status == 200:
                reply["elapsed_ms"] = int((time.monotonic() - started) * 1000)
                code = "ok"
                self._send(200, reply)
            else:
                code = reply.get("error", {}).get("code", "internal")
                self._send(status, reply)
        except ApiError as e:
            status, code, reply = e.status, e.code, e.body()
            try:
                self._send(e.status, e.body(), e.headers)
            except OSError:                         # the client went while its body was dropped
                self.close_connection = True
        except BodyLate:
            e = ApiError(408, "body_timeout", f"the body did not arrive within {self.body_s} s")
            status, code, reply = e.status, e.code, e.body()
            self.close_connection = True
            try:
                self._send(e.status, e.body())
            except OSError:
                pass
        except ClientGone:
            status, code = 0, "client_gone"
            self.close_connection = True
        except Exception as e:                      # e.g. /tmp full: answer 500, log the type only
            status, code, note = 500, "internal", type(e).__name__
            reply = {"error": {"code": "internal", "message": "the decoder failed"}}
            self.close_connection = True
            try:
                self._send(500, reply)
            except OSError:
                pass
        finally:
            self._log_job("decode", kind, size, reply, status, code, started, note)

    def _log_job(self, what, kind, size, reply, status, code, started, note=""):
        """One line, never a decoded text: type, bytes, pixels, pages, codes, ms, status, error code."""
        err = reply.get("error", {}) if isinstance(reply, dict) else {}
        w = reply.get("width", err.get("width")) if isinstance(reply, dict) else None
        h = reply.get("height", err.get("height")) if isinstance(reply, dict) else None
        pixels = f"{w}x{h}" if isinstance(w, int) and isinstance(h, int) else "-"
        pages = reply.get("pages") if isinstance(reply, dict) and reply.get("pages") is not None else "-"
        codes = len(reply["barcodes"]) if isinstance(reply, dict) and isinstance(reply.get("barcodes"), list) else "-"
        ms = int((time.monotonic() - started) * 1000)
        line = (f"{what} type={kind} bytes={size} pixels={pixels} pages={pages} codes={codes} ms={ms} "
                f"status={status} code={code}")
        if note:
            line += f" note={re.sub(r'[^A-Za-z0-9_ -]', '', note)[:60]}"
        log(line)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 64

    def handle_error(self, request, client_address):
        """One line with the exception's type instead of the stdlib's traceback on stderr."""
        log(f"request error: {sys.exc_info()[0].__name__}")


def main():
    decoder = Decoder()
    try:
        srv = Server((settings.HOST, settings.PORT), Handler)
    except OSError as e:
        log(f"cannot listen on {settings.HOST}:{settings.PORT}: {e.strerror}")
        return 1
    srv.decoder = decoder
    host, port = srv.server_address[:2]
    info = decoder.info
    log(f"{NAME} {__version__} listening on {host}:{port} (api {API}; zxing-cpp {info['engine']['version']}; "
        f"pdftoppm {info['pdf']['version']}; libheif {(info['heif'] or {}).get('version')}; "
        f"types {','.join(decoder.types)})")

    def stop(signum, frame):
        threading.Thread(target=srv.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        srv.serve_forever(poll_interval=0.5)
    finally:
        decoder.stop()
        srv.server_close()
        log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
