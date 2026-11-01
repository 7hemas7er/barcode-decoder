"""Shared by the decoder tests: the fictional BCBP, images of barcodes, and a decoder server in a subprocess.

Every fixture is fictional: bcbp() is a copy of boarding-pass-tracker's test_flights.bcbp() (ROSSI/MARIO, FR 1234
BLQ-STN), the default of all its tests.
"""
import http.client, io, json, os, re, subprocess, sys, threading, time, zlib

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")
FAKEBIN = os.path.join(HERE, "fakebin")


def bcbp(name="ROSSI/MARIO MR", pnr="XYZ789", origin="BLQ", destination="STN", airline="FR", number="1234", day=310,
         seat="012A", seq="0042", others=(), cabin="Y"):
    """A BCBP with the first leg and the others (tuples origin, destination, airline, number, day, seat), without
    the variable part."""
    def leg(pnr, origin, destination, airline, number, day, seat, seq):
        return f"{pnr:<7}{origin}{destination}{airline:<3}{number:0>4} {day:03d}{cabin}{seat:>4}{seq:<5}100"
    head = f"M{1 + len(others)}{name:<20}E"
    body = leg(pnr, origin, destination, airline, number, day, seat, seq)
    for t in others:
        body += leg(pnr, *t, "0043")
    return head + body


BCBP = bcbp()
# Pieces of the fictional BCBP that must never reach a log
SECRETS = (BCBP, "ROSSI", "MARIO", "XYZ789")


def code_image(text=BCBP, fmt="Aztec", scale=4, border=40):
    """A PIL image ("L") of the barcode `text` in zxing-cpp format `fmt`, written by zxing-cpp, with a white
    border."""
    import zxingcpp
    from PIL import Image
    image = zxingcpp.write_barcode_to_image(zxingcpp.create_barcode(text, getattr(zxingcpp.BarcodeFormat, fmt)),
                                            scale=scale)
    view = memoryview(image)
    code = Image.frombuffer("L", (view.shape[1], view.shape[0]), bytes(view), "raw", "L", 0, 1)
    out = Image.new("L", (code.width + 2 * border, code.height + 2 * border), 255)
    out.paste(code, (border, border))
    return out


def encode(image, kind, **k):
    buf = io.BytesIO()
    image.save(buf, kind, **k)
    return buf.getvalue()


def huge(kind, side=9000, height=None):
    """A 16x16 JPEG or PNG whose header says side x height (default side): PIL reads the size, decoding it would
    fail. (From tracker/test_barcodes.py, with a separate height.)"""
    from PIL import Image
    height = side if height is None else height
    buf = io.BytesIO()
    Image.new("RGB", (16, 16)).save(buf, kind)
    b = bytearray(buf.getvalue())
    if kind == "JPEG":
        i = b.index(b"\xff\xc0")                                    # SOF0: height and width after length, precision
        b[i + 5:i + 9] = height.to_bytes(2, "big") + side.to_bytes(2, "big")
    else:
        b[16:24] = side.to_bytes(4, "big") + height.to_bytes(4, "big")    # IHDR, then its CRC
        b[29:33] = zlib.crc32(bytes(b[12:29])).to_bytes(4, "big")
    return bytes(b)


def fixture(name):
    with open(os.path.join(FIXTURES, name), "rb") as f:
        return f.read()


# ---------------------------------------------------------------- a decoder server in a subprocess
class DecoderProcess:
    """`python -I -B -m bcdecoder.server` on 127.0.0.1 and a free port, with extra environment (limits, PATH).
    Its stdout and stderr are collected in .log (T-D10 reads it)."""

    def __init__(self, env=None, path_prefix=None):
        self.env = dict(os.environ, BCDECODER_HOST="127.0.0.1", BCDECODER_PORT="0", **(env or {}))
        if path_prefix:
            self.env["PATH"] = path_prefix + os.pathsep + self.env.get("PATH", "")
        self.log = ""
        self._lines = []

    def __enter__(self):
        self.proc = subprocess.Popen([sys.executable, "-I", "-B", "-m", "bcdecoder.server"], env=self.env,
                                     stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True)
        self._ready = threading.Event()
        self.port = None
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()
        if not self._ready.wait(30) or self.port is None:
            self.proc.kill()
            raise RuntimeError("the decoder did not start: " + "".join(self._lines))
        return self

    def _read(self):
        for line in self.proc.stdout:
            self._lines.append(line)
            m = re.search(r"listening on [0-9.]+:(\d+)", line)
            if m and self.port is None:
                self.port = int(m.group(1))
                self._ready.set()
        self._ready.set()

    def __exit__(self, *exc):
        self.proc.terminate()
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        self._reader.join(5)
        self.proc.stdout.close()
        self.log = "".join(self._lines)

    def text(self):
        return "".join(self._lines)

    # ------------------------------------------------------------ requests
    def request(self, method, path, body=None, headers=None, timeout=90):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            r = conn.getresponse()
            data = r.read()
            try:
                payload = json.loads(data)
            except ValueError:
                payload = None
            return r.status, payload, dict(r.getheaders())
        finally:
            conn.close()

    def decode(self, data, query="", timeout=90):
        path = "/v1/decode" + (("?" + query) if query else "")
        return self.request("POST", path, body=data, headers={"Content-Type": "application/octet-stream"},
                            timeout=timeout)

    def health(self, query=""):
        return self.request("GET", "/health" + (("?" + query) if query else ""))


def texts(payload):
    return [b["text"] for b in (payload or {}).get("barcodes", [])]


def wait_until(predicate, timeout=10, step=0.05):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(step)
    return predicate()
