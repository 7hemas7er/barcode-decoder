"""T3 at the HTTP level, against `python -I -B -m bcdecoder.server` in a subprocess (plan §7.3, MDM B.9 adapted to
API v1 §5.9): T-D1..T-D8 and T-D10, plus /health, the self-test, the routes, the query parameters and the
slow-body limits (SlowBodies, review finding SECURITY-03). T-D9 runs in
the integration's workflow; T-D11 (the container) is tests/host/test_container.py. HEIC: test_heic.py.

  python -I -B -m unittest discover -s /tests -t /tests -v          (inside the image: see tests/run_t3.sh)
"""
import base64, concurrent.futures, json, os, re, shutil, socket, tempfile, threading, time, unittest

from PIL import Image

import support as s

BLANK_A4 = (1240, 1754)                                  # A4 at 150 dpi
LIMITS = {"max_bytes": 33554432, "max_pixels": 50000000, "max_pages": 10, "default_pages": 3, "pdf_side": 2400,
          "max_codes": 50, "text_max": 8192, "timeout_s": 60, "queue": 4}
LOG_LINE = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d (decode|selftest) type=\S+ bytes=\d+ pixels=\S+ pages=\S+ "
                      r"codes=\S+ ms=\d+ status=\d+ code=\w+( note=[A-Za-z0-9_ -]*)?$")


def pdf_with_code_on(pages, code_page, code=None, codes=None):
    """A PDF of `pages` A4 pages; the Aztec of the BCBP on page `code_page` (1-based), or `codes` {page: image}."""
    codes = dict(codes or {})
    if code_page:
        codes[code_page] = code or s.code_image()
    images = []
    for n in range(1, pages + 1):
        page = Image.new("L", BLANK_A4, 255)
        if n in codes:
            page.paste(codes[n], (100, 100))
        images.append(page)
    return s.encode(images[0], "PDF", save_all=True, append_images=images[1:], resolution=150)


def raw_request(port, head, body=b"", timeout=10):
    """Send raw bytes, return (status, headers dict, body bytes, seconds until the answer)."""
    sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    try:
        start = time.monotonic()
        sock.sendall(head + body)
        data = b""
        while True:
            try:
                chunk = sock.recv(65536)
            except ConnectionResetError:
                break
            if not chunk:
                break
            data += chunk
        took = time.monotonic() - start
    finally:
        sock.close()
    head_, _, payload = data.partition(b"\r\n\r\n")
    lines = head_.decode("latin-1").split("\r\n")
    status = int(lines[0].split()[1])
    headers = {k.strip().lower(): v.strip() for k, v in (line.split(":", 1) for line in lines[1:] if ":" in line)}
    return status, headers, payload, took


def assert_clean_log(test, log):
    """T-D10: no decoded text in the log, and every line is a start line, a job line or the stop line."""
    for secret in s.SECRETS:
        test.assertNotIn(secret, log)
    for line in log.splitlines():
        if " listening on " in line or line.endswith(" stopped"):
            continue
        test.assertRegex(line, LOG_LINE)


class Api(unittest.TestCase):
    """One server for the quick tests."""

    @classmethod
    def setUpClass(cls):
        cls.dec = s.DecoderProcess()
        cls.dec.__enter__()
        cls.aztec_png = s.encode(s.code_image(), "PNG")

    @classmethod
    def tearDownClass(cls):
        cls.dec.__exit__(None, None, None)

    def ok(self, data, query="", count=None):
        status, body, _ = self.dec.decode(data, query)
        self.assertEqual(status, 200, body)
        if count is not None:
            self.assertEqual(len(body["barcodes"]), count, body)
        return body

    def refused(self, data, status, code, query=""):
        got, body, headers = self.dec.decode(data, query)
        self.assertEqual((got, body["error"]["code"]), (status, code), body)
        self.assertEqual(headers.get("Content-Type"), "application/json")
        return body

    # ------------------------------------------------------------ /health and the self-test (part of T-D11)
    def test_health(self):
        status, body, headers = self.dec.health()
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "application/json")
        self.assertEqual(set(body), {"ok", "api", "name", "version", "engine", "pdf", "heif", "types", "limits"})
        self.assertEqual((body["ok"], body["api"], body["name"], body["version"]),
                         (True, 1, "barcode-decoder", "1.0.0"))
        self.assertEqual(body["engine"], {"name": "zxing-cpp", "version": "3.1.1"})
        self.assertEqual(body["pdf"]["renderer"], "pdftoppm")
        self.assertRegex(body["pdf"]["version"], r"^\d+\.\d+")
        self.assertEqual(body["limits"], LIMITS)
        self.assertEqual(body["types"], ["jpeg", "png", "webp", "pdf", "heic"])
        self.assertEqual(body["heif"]["library"], "libheif")
        self.assertEqual(body["heif"]["decoders"], ["libde265"])
        self.assertNotIn("selftest", body)

    def test_selftest(self):
        status, body, _ = self.dec.health("selftest=1")
        self.assertEqual(status, 200)
        self.assertEqual(body["selftest"], "ok", body)
        self.assertEqual(body["selftest_checks"], {"png": "ok", "pdf": "ok", "heic": "ok"})

    # ------------------------------------------------------------ T-D1: formats at 0/90/180/270 degrees
    def test_d1_formats_at_four_angles(self):
        for fmt in ("Aztec", "PDF417", "QRCode"):
            code = s.code_image(s.BCBP, fmt, scale=4 if fmt != "PDF417" else 3)
            for angle in (0, 90, 180, 270):
                with self.subTest(fmt=fmt, angle=angle):
                    im = code.rotate(angle, expand=True, fillcolor=255)
                    body = self.ok(s.encode(im, "PNG"), count=1)
                    b = body["barcodes"][0]
                    self.assertEqual((b["format"], b["text"], b["page"], b["valid"]), (fmt, s.BCBP, None, True))
                    self.assertEqual(base64.b64decode(b["bytes_b64"]).decode(), s.BCBP)
                    self.assertEqual(b["content_type"], "Text")
                    self.assertRegex(b["position"], r"^\d+x\d+ \d+x\d+ \d+x\d+ \d+x\d+$")
                    self.assertIsInstance(b["orientation"], int)
                    self.assertTrue(b["symbology_identifier"].startswith("]"))
                    self.assertEqual((body["api"], body["type"], body["width"], body["height"], body["pages"],
                                      body["truncated"]), (1, "png", im.width, im.height, None, False))
                    self.assertIsInstance(body["elapsed_ms"], int)

    def test_d1_jpeg_and_webp(self):
        code = s.code_image().convert("RGB")
        for kind, name in (("JPEG", "jpeg"), ("WEBP", "webp")):
            with self.subTest(kind=kind):
                body = self.ok(s.encode(code, kind, quality=95), count=1)
                self.assertEqual((body["type"], body["barcodes"][0]["text"]), (name, s.BCBP))

    def test_d1_formats_parameter(self):
        self.assertEqual(self.ok(self.aztec_png, "formats=QRCode")["barcodes"], [])
        self.assertEqual(s.texts(self.ok(self.aztec_png, "formats=qrcode,AZTEC")), [s.BCBP])
        self.assertEqual(s.texts(self.ok(self.aztec_png, "formats=Aztec")), [s.BCBP])

    def test_no_dedup_and_max_codes(self):
        qr = [s.code_image(f"CODE-{i}", "QRCode", scale=4, border=20) for i in range(3)]
        sheet = Image.new("L", (sum(q.width for q in qr) + 40, qr[0].height + 40), 255)
        x = 20
        for q in qr:
            sheet.paste(q, (x, 20))
            x += q.width
        data = s.encode(sheet, "PNG")
        self.assertEqual(sorted(s.texts(self.ok(data, count=3))), ["CODE-0", "CODE-1", "CODE-2"])
        body = self.ok(data, "max_codes=2", count=2)
        self.assertTrue(body["truncated"])
        same = Image.new("L", (qr[0].width * 2 + 40, qr[0].height + 40), 255)
        same.paste(qr[0], (20, 20))
        same.paste(qr[0], (20 + qr[0].width, 20))
        self.assertEqual(s.texts(self.ok(s.encode(same, "PNG"))), ["CODE-0", "CODE-0"])   # the caller de-dups

    # ------------------------------------------------------------ T-D2: EXIF orientation
    def test_d2_exif_orientation_is_not_applied_and_the_code_is_read(self):
        canvas = Image.new("L", (600, 300), 255)
        canvas.paste(s.code_image(scale=3, border=10), (20, 20))
        for orientation, angle in ((6, 90), (8, 270), (3, 180)):
            with self.subTest(orientation=orientation):
                stored = canvas.rotate(angle, expand=True, fillcolor=255).convert("RGB")
                exif = Image.Exif()
                exif[0x0112] = orientation
                body = self.ok(s.encode(stored, "JPEG", quality=95, exif=exif), count=1)
                self.assertEqual(body["barcodes"][0]["text"], s.BCBP)
                self.assertEqual((body["width"], body["height"]), stored.size)          # no EXIF transpose

    # ------------------------------------------------------------ T-D3: pages
    def test_d3_code_on_page_4(self):
        pdf = pdf_with_code_on(5, 4)
        body = self.ok(pdf, count=0)
        self.assertEqual((body["type"], body["pages"], body["width"], body["height"]), ("pdf", 3, None, None))
        body = self.ok(pdf, "pages=4", count=1)
        self.assertEqual((body["pages"], body["barcodes"][0]["page"], body["barcodes"][0]["text"]), (4, 4, s.BCBP))
        body = self.ok(pdf, "pages=10", count=1)
        self.assertEqual(body["pages"], 5)                                             # only 5 exist

    def test_d3_pages_in_order(self):
        codes = {2: s.code_image("PAGE-TWO", "QRCode"), 1: s.code_image("PAGE-ONE", "QRCode")}
        body = self.ok(pdf_with_code_on(3, None, codes=codes), count=2)
        self.assertEqual([(b["page"], b["text"]) for b in body["barcodes"]], [(1, "PAGE-ONE"), (2, "PAGE-TWO")])

    # ------------------------------------------------------------ T-D4: the pixel guard
    def test_d4_header_over_the_limit_is_refused_fast(self):
        for kind in ("JPEG", "PNG"):
            for w, h in ((7072, 7071), (20000, 20000)):               # 50,006,112 px; 400 Mpx (beyond PIL's own)
                with self.subTest(kind=kind, size=(w, h)):
                    start = time.monotonic()
                    body = self.refused(s.huge(kind, w, h), 422, "too_many_pixels")
                    took = time.monotonic() - start
                    self.assertEqual((body["error"]["width"], body["error"]["height"]), (w, h))
                    self.assertLess(took, 1.5)

    def test_d4_the_limit(self):
        # exactly 50,000,000: the guard lets it through, and the 16x16 data behind the header cannot be decoded
        self.refused(s.huge("PNG", 10000, 5000), 422, "unreadable")
        small = s.encode(Image.new("L", (16, 16), 255), "PNG")
        body = self.refused(small, 422, "too_many_pixels", "pixel_limit=255")
        self.assertEqual((body["error"]["width"], body["error"]["height"]), (16, 16))
        self.ok(small, "pixel_limit=256", count=0)

    # ------------------------------------------------------------ T-D5: unreadable and unsupported
    def test_d5_unreadable(self):
        jpeg = s.encode(s.code_image().convert("RGB"), "JPEG", quality=90)
        for name, data in (("truncated jpeg", jpeg[:len(jpeg) // 2]), ("broken pdf", b"%PDF-1.7 broken"),
                           ("png magic, no image", b"\x89PNG\r\n\x1a\n" + b"\x00" * 64),
                           ("webp magic, no image", b"RIFF\x20\x00\x00\x00WEBPVP8 " + b"\x00" * 24)):
            with self.subTest(name):
                self.refused(data, 422, "unreadable")

    def test_d5_unsupported_types(self):
        im = Image.new("RGB", (32, 32), "white")
        for name, data in (("xml", b'<?xml version="1.0"?><a/>'),
                           ("svg", b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>'),
                           ("postscript", b"%!PS-Adobe-3.0\n%%EOF\n"),
                           ("multipart", b'--xyz\r\nContent-Disposition: form-data; name="file"\r\n\r\n'
                                         + self.aztec_png + b"\r\n--xyz--\r\n"),
                           ("gif", s.encode(im, "GIF")), ("bmp", s.encode(im, "BMP")),
                           ("tiff", s.encode(im, "TIFF")), ("zip/pkpass", b"PK\x03\x04" + b"\x00" * 60),
                           ("text", b"hello")):
            with self.subTest(name):
                body = self.refused(data, 415, "unsupported_type")
                self.assertIn("heic", body["error"]["message"])

    # ------------------------------------------------------------ T-D6: 413 before reading, 400
    def test_d6_too_large_before_the_body(self):
        head = (b"POST /v1/decode HTTP/1.1\r\nHost: x\r\nContent-Type: application/octet-stream\r\n"
                b"Content-Length: %d\r\n\r\n" % (40 << 20))
        status, headers, payload, took = raw_request(self.dec.port, head)          # no body is ever sent
        self.assertEqual(status, 413)
        self.assertIn(b'"too_large"', payload)
        self.assertIn(b'"max_bytes":33554432', payload)
        self.assertLess(took, 3)

    def test_d6_empty_body(self):
        for head in (b"POST /v1/decode HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n",
                     b"POST /v1/decode HTTP/1.1\r\nHost: x\r\n\r\n",
                     b"POST /v1/decode HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n"):
            with self.subTest(head=head):
                status, _, payload, _ = raw_request(self.dec.port, head)
                self.assertEqual(status, 400)
                self.assertIn(b'"empty_body"', payload)
        status, _, payload, _ = raw_request(self.dec.port, b"POST /v1/decode HTTP/1.1\r\nHost: x\r\n"
                                                           b"Content-Length: -5\r\n\r\n")
        self.assertEqual(status, 400)
        self.assertIn(b'"bad_param"', payload)

    def test_bad_params(self):
        for query in ("pages=0", "pages=11", "pages=x", "pages=", "pages=-1", "max_codes=0", "max_codes=51",
                      "pixel_limit=0", "pixel_limit=50000001", "formats=", "formats=bogus", "formats=Aztec,,QRCode",
                      "formats=NONE", "other=1", "pages=1&pages=2"):
            with self.subTest(query=query):
                self.refused(self.aztec_png, 400, "bad_param", query)

    def test_routes(self):
        status, body, headers = self.dec.request("GET", "/v1/decode")
        self.assertEqual((status, body["error"]["code"], headers.get("Allow")), (405, "method_not_allowed", "POST"))
        status, body, headers = self.dec.request("POST", "/health", body=b"x")
        self.assertEqual((status, headers.get("Allow")), (405, "GET"))
        self.assertEqual(self.dec.request("GET", "/nope")[0], 404)
        self.assertEqual(self.dec.request("PUT", "/v1/decode", body=b"x")[0], 405)
        status, headers, payload, _ = raw_request(self.dec.port, b"BREW /v1/decode HTTP/1.1\r\nHost: x\r\n\r\n")
        self.assertEqual(status, 501)
        self.assertIn(b'"not_implemented"', payload)
        self.assertEqual(headers["content-type"], "application/json")
        status, body, headers = self.dec.health()
        self.assertTrue(headers.get("Server", "").startswith("barcode-decoder/1.0.0"))

    def test_zz_d10_log_holds_no_decoded_text(self):
        self.ok(self.aztec_png, count=1)
        self.ok(pdf_with_code_on(1, 1), count=1)
        time.sleep(0.2)
        log = self.dec.text()
        self.assertIn("decode type=png", log)
        self.assertIn("decode type=pdf", log)
        assert_clean_log(self, log)


def _sleepers():
    """Processes of the fake pdftoppm (sleep 30 / sleep 31) still alive in this PID namespace."""
    out = []
    for pid in os.listdir("/proc"):
        if pid.isdigit():
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    cmd = f.read().split(b"\0")
                with open(f"/proc/{pid}/stat") as f:
                    state = f.read().rsplit(")", 1)[1].split()[0]
            except OSError:
                continue
            if cmd[:1] == [b"sleep"] and cmd[1:2] in ([b"30"], [b"31"]) and state != "Z":
                out.append(int(pid))
    return out


class Timeouts(unittest.TestCase):
    """T-D7 (504, the job's processes killed, the next request works) and T-D8 (one 503 out of six)."""

    def test_d7_slow_pdftoppm_gives_504_and_the_next_request_works(self):
        for fake in ("slow", "stuck"):                       # stuck: pdftoppm leaves a background child behind
            with self.subTest(fake=fake), s.DecoderProcess(env={"BCDECODER_TIMEOUT_S": "3"},
                                                           path_prefix=os.path.join(s.FAKEBIN, fake)) as dec:
                start = time.monotonic()
                status, body, _ = dec.decode(pdf_with_code_on(1, 1))
                took = time.monotonic() - start
                self.assertEqual((status, body["error"]["code"]), (504, "timeout"), body)
                self.assertLess(took, 8)
                self.assertTrue(s.wait_until(lambda: not _sleepers(), 5), _sleepers())
                status, body, _ = dec.decode(s.encode(s.code_image(), "PNG"))
                self.assertEqual((status, s.texts(body)), (200, [s.BCBP]))
                status, body, _ = dec.health()
                self.assertEqual(status, 200)
            assert_clean_log(self, dec.log)

    def test_d7_a_decode_past_the_time_is_killed(self):
        # 4000x4000 of noise: zxing-cpp needs well over a second of CPU on it; the job gets 1 s
        noise = Image.frombytes("L", (4000, 4000), os.urandom(4000 * 4000))
        data = s.encode(noise, "PNG", compress_level=1)
        with s.DecoderProcess(env={"BCDECODER_TIMEOUT_S": "1"}) as dec:
            start = time.monotonic()
            status, body, _ = dec.decode(data)
            took = time.monotonic() - start
            self.assertEqual((status, body["error"]["code"]), (504, "timeout"), body)
            self.assertLess(took, 6)
            status, body, _ = dec.decode(s.encode(s.code_image(), "PNG"))
            self.assertEqual((status, s.texts(body)), (200, [s.BCBP]))
        self.assertRegex(dec.log, r"status=504 code=timeout note=(killed|SIGXCPU)")

    def test_d8_six_in_parallel_one_gets_503(self):
        pdf = pdf_with_code_on(1, 1)
        with s.DecoderProcess(env={"BCDECODER_TIMEOUT_S": "20"},
                              path_prefix=os.path.join(s.FAKEBIN, "sleepy")) as dec:
            barrier = threading.Barrier(6)

            def one(_):
                barrier.wait()
                return dec.decode(pdf, timeout=60)
            with concurrent.futures.ThreadPoolExecutor(6) as pool:
                results = list(pool.map(one, range(6)))
        statuses = sorted(r[0] for r in results)
        self.assertEqual(statuses, [200, 200, 200, 200, 200, 503], [(r[0], r[1]) for r in results])
        (busy,) = [r for r in results if r[0] == 503]
        self.assertEqual((busy[1]["error"]["code"], busy[2].get("Retry-After")), ("busy", "30"))
        for status, body, _ in results:
            if status == 200:
                self.assertEqual((body["pages"], body["barcodes"]), (0, []))
        assert_clean_log(self, dec.log)


class Trickle:
    """A client that sends a Content-Length and the bytes `first`, then one more byte every `step` seconds (from a
    thread) until it is closed or the server goes."""

    def __init__(self, port, length, first=b"\x89", step=1.0):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=30)
        self.sock.sendall(b"POST /v1/decode HTTP/1.0\r\nContent-Length: %d\r\n\r\n" % length + first)
        self.sent, self.stop = len(first), threading.Event()
        self.thread = threading.Thread(target=self._run, args=(step,), daemon=True)
        self.thread.start()

    def _run(self, step):
        while not self.stop.wait(step):
            try:
                self.sock.sendall(b"P")
            except OSError:
                return
            self.sent += 1

    def answer(self, timeout):
        """(status, error code) of the server's answer, or (None, None) when it closed without one."""
        self.sock.settimeout(timeout)
        data = b""
        try:
            while True:
                chunk = self.sock.recv(65536)
                if not chunk:
                    break
                data += chunk
        except (ConnectionResetError, TimeoutError):
            pass
        head, _, body = data.partition(b"\r\n\r\n")
        if not head.startswith(b"HTTP/"):
            return None, None
        return int(head.split()[1]), json.loads(body)["error"]["code"]

    def close(self):
        self.stop.set()
        self.thread.join(5)
        self.sock.close()


class SlowBodies(unittest.TestCase):
    """SECURITY-03: a body is received before its job takes one of the 1 + QUEUE slots, so slow senders hold
    connections, never job slots; one deadline for the whole body, max(BODY_MIN_S, bytes / 256 KiB/s), then 408
    body_timeout; the bodies on /tmp at once are bounded by 1 + QUEUE bodies, counted as the bytes arrive (503 busy
    beyond), and given back when a request ends."""

    @classmethod
    def setUpClass(cls):
        cls.png = s.encode(s.code_image(), "PNG")

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_slow_senders_hold_no_job_slot(self):
        with s.DecoderProcess(env={"BCDECODER_TMP": self.tmp}) as dec:
            slow = [Trickle(dec.port, 100000) for _ in range(1 + 4 + 2)]      # more senders than the 5 job slots
            try:
                time.sleep(1.5)
                for _ in range(3):                                            # the 8th, 9th, 10th connection
                    status, body, _ = dec.decode(self.png, timeout=20)
                    self.assertEqual((status, s.texts(body)), (200, [s.BCBP]), body)
                status, body, _ = dec.health("selftest=1")                    # it takes a job slot too
                self.assertEqual(body["selftest"], "ok", body)
                self.assertTrue(all(t.sent >= 2 and t.thread.is_alive() for t in slow), [t.sent for t in slow])
            finally:
                for t in slow:
                    t.close()
            self.assertTrue(s.wait_until(lambda: dec.text().count("code=client_gone") == len(slow), 10), dec.text())
            self.assertTrue(s.wait_until(lambda: os.listdir(self.tmp) == [], 5), os.listdir(self.tmp))
        self.assertEqual(len(re.findall(r"decode type=- bytes=100000 .* status=0 code=client_gone", dec.log)),
                         len(slow))
        assert_clean_log(self, dec.log)

    def test_a_body_slower_than_its_deadline_gets_408(self):
        with s.DecoderProcess(env={"BCDECODER_BODY_MIN_S": "2", "BCDECODER_TMP": self.tmp}) as dec:
            t = Trickle(dec.port, 100000, step=0.25)                          # 4 bytes a second, never idle
            start = time.monotonic()
            try:
                answer = t.answer(timeout=15)
                took = time.monotonic() - start
            finally:
                t.close()
            self.assertEqual(answer, (408, "body_timeout"))
            self.assertTrue(1.8 <= took < 5, took)                          # 100000 bytes: max(2 s, 1 s)
            status, body, _ = dec.decode(self.png)
            self.assertEqual((status, s.texts(body)), (200, [s.BCBP]))
        self.assertRegex(dec.log, r"decode type=- bytes=100000 pixels=- pages=- codes=- ms=\d+ status=408 "
                                  r"code=body_timeout")
        self.assertEqual(os.listdir(self.tmp), [])
        assert_clean_log(self, dec.log)

    def test_bodies_on_tmp_are_bounded_and_given_back(self):
        mib = 1 << 20
        env = {"BCDECODER_MAX_BYTES": str(mib), "BCDECODER_QUEUE": "0", "BCDECODER_BODY_MIN_S": "3",
               "BCDECODER_TMP": self.tmp}
        with s.DecoderProcess(env=env) as dec:                             # budget: (1 + 0) bodies of 1 MiB
            start = time.monotonic()
            t = Trickle(dec.port, mib, first=b"\x89PNG" + b"\0" * (mib - 64 - 4), step=3600)   # all but 64 bytes
            try:
                self.assertTrue(s.wait_until(lambda: sum(os.path.getsize(os.path.join(r, f))
                                                         for r, _, fs in os.walk(self.tmp) for f in fs)
                                             == mib - 64, 5))
                status, body, headers = dec.decode(self.png)               # more than the 64 bytes left
                self.assertEqual((status, body["error"]["code"], headers.get("Retry-After")), (503, "busy", "30"))
                answer = t.answer(timeout=15)
                took = time.monotonic() - start
            finally:
                t.close()
            self.assertEqual(answer, (408, "body_timeout"))
            self.assertTrue(3.6 <= took < 7, took)                          # 1 MiB: max(3 s, 4 s), not 3 s
            status, body, _ = dec.decode(self.png)                         # its bytes were given back
            self.assertEqual((status, s.texts(body)), (200, [s.BCBP]))
        self.assertRegex(dec.log, rf"decode type=- bytes={len(self.png)} .* status=503 code=busy")
        self.assertRegex(dec.log, rf"decode type=- bytes={mib} .* status=408 code=body_timeout")
        self.assertEqual(os.listdir(self.tmp), [])
        assert_clean_log(self, dec.log)


class Crash(unittest.TestCase):
    """A child that dies or runs out of memory: 500 internal, and the server goes on."""

    def test_out_of_memory_gives_500_internal(self):
        big = s.encode(Image.new("RGB", (6000, 6000), "white"), "PNG")             # ~108 MB once decoded
        with s.DecoderProcess(env={"BCDECODER_MEMORY_MB": "160"}) as dec:
            status, body, _ = dec.decode(big)
            self.assertEqual((status, body["error"]["code"]), (500, "internal"), body)
            status, body, _ = dec.decode(s.encode(s.code_image(), "PNG"))
            self.assertEqual((status, s.texts(body)), (200, [s.BCBP]))
        assert_clean_log(self, dec.log)


class LogPrivacy(unittest.TestCase):
    """T-D10 over every outcome: the server's whole log, after it stopped."""

    def test_d10_no_decoded_text_in_the_log(self):
        code = s.code_image()
        with s.DecoderProcess() as dec:
            requests = [(s.encode(code, "PNG"), 200), (s.encode(code.convert("RGB"), "JPEG"), 200),
                        (pdf_with_code_on(2, 2), 200), (s.fixture("aztec.heic"), 200),
                        (s.huge("PNG"), 422), (b"hello " + s.BCBP.encode(), 415), (b"%PDF-1.7 broken", 422)]
            for data, expected in requests:
                self.assertEqual(dec.decode(data)[0], expected)
            self.assertEqual(dec.health("selftest=1")[1]["selftest"], "ok")
        log = dec.log
        assert_clean_log(self, log)
        self.assertEqual(len(re.findall(r" decode type=", log)), len(requests))
        self.assertEqual(len(re.findall(r" selftest type=", log)), 1)
        self.assertRegex(log, r"decode type=heic bytes=\d+ pixels=800x600 pages=- codes=1 ms=\d+ status=200 code=ok")
        self.assertRegex(log, r"decode type=pdf bytes=\d+ pixels=- pages=2 codes=1 ")
        self.assertRegex(log, r"decode type=png bytes=\d+ pixels=9000x9000 pages=- codes=- ms=\d+ status=422 "
                              r"code=too_many_pixels")


if __name__ == "__main__":
    unittest.main()
