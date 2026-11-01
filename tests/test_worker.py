"""In process, without HTTP: the pixel guard before decoding (T-D4's "no decoding"), the reply objects, the mapping
of a finished child to an answer (504/500), and the query parameters.
"""
import os, signal, tempfile, time, types, unittest
from unittest import mock

import support as s
from bcdecoder import engine, server, settings, worker


def job(tmp, data, kind, **k):
    with open(os.path.join(tmp, "in"), "wb") as f:
        f.write(data)
    return {"type": kind, "pages": 3, "formats": None, "max_codes": 50, "pixel_limit": settings.MAX_PIXELS,
            "pdf_side": 2400, "text_max": 8192, "timeout_s": 60, **k}


class Guard(unittest.TestCase):
    def test_d4_refused_from_the_header_without_decoding(self):
        for kind, name in (("JPEG", "jpeg"), ("PNG", "png")):
            for w, h in ((7072, 7071), (20000, 20000)):
                with self.subTest(kind=kind, size=(w, h)), tempfile.TemporaryDirectory() as tmp:
                    j = job(tmp, s.huge(kind, w, h), name)
                    with mock.patch.object(engine, "results", side_effect=AssertionError("decoded")):
                        with self.assertRaises(worker.JobError) as e:
                            worker.decode(tmp, j, time.monotonic())
                    self.assertEqual((e.exception.status, e.exception.code, e.exception.extra),
                                     (422, "too_many_pixels", {"width": w, "height": h}))

    def test_pil_limit_restored(self):
        saved = engine.Image.MAX_IMAGE_PIXELS
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "in")
            with open(path, "wb") as f:
                f.write(s.huge("PNG", 20000, 20000))
            self.assertEqual(engine.header_size(path, "png"), (20000, 20000))
        self.assertEqual(engine.Image.MAX_IMAGE_PIXELS, saved)

    def test_formats_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            j = job(tmp, s.encode(s.code_image(), "PNG"), "png", formats="bogus")
            with self.assertRaises(worker.JobError) as e:
                worker.decode(tmp, j, time.monotonic())
        self.assertEqual((e.exception.status, e.exception.code), (400, "bad_param"))


class Reply(unittest.TestCase):
    def fake(self, text, raw):
        enum = types.SimpleNamespace
        return types.SimpleNamespace(text=text, bytes=raw, format=enum(name="QRCode"), content_type=enum(name="Text"),
                                     position="1x1 2x1 2x2 1x2", orientation=90, symbology_identifier="]Q1",
                                     valid=True)

    def test_text_and_bytes_cut_at_text_max(self):
        item, cut = worker.barcode(self.fake("é" * 20, ("é" * 20).encode()), 2, text_max=10)
        self.assertTrue(cut)
        self.assertEqual((item["text"], item["page"], item["format"], item["orientation"]), ("é" * 10, 2, "QRCode", 90))
        self.assertEqual(len(__import__("base64").b64decode(item["bytes_b64"])), 10)
        item, cut = worker.barcode(self.fake("abc", b"abc"), None, text_max=8192)
        self.assertFalse(cut)
        self.assertEqual(set(item), {"page", "format", "text", "bytes_b64", "content_type", "position", "orientation",
                                     "symbology_identifier", "valid"})

    def test_decode_reply_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            body = worker.decode(tmp, job(tmp, s.encode(s.code_image(), "PNG"), "png"), time.monotonic())
        self.assertEqual(set(body), {"api", "type", "width", "height", "pages", "truncated", "elapsed_ms", "barcodes"})
        self.assertEqual([b["text"] for b in body["barcodes"]], [s.BCBP])


class ChildOutcome(unittest.TestCase):
    def test_answers(self):
        ok = {"status": 200, "body": {"api": 1, "barcodes": []}}
        self.assertEqual(server.child_outcome(0, ok, False)[:2], (200, {"api": 1, "barcodes": []}))
        err = {"status": 422, "error": {"code": "unreadable", "message": "x"}}
        self.assertEqual(server.child_outcome(0, err, False)[:2], (422, {"error": err["error"]}))
        internal = {"status": 500, "error": {"code": "internal", "message": "x"}, "exc": "MemoryError"}
        self.assertEqual(server.child_outcome(0, internal, False), (500, {"error": internal["error"]}, "MemoryError"))

    def test_timeouts(self):
        self.assertEqual(server.child_outcome(-signal.SIGKILL, None, True)[0], 504)
        self.assertEqual(server.child_outcome(-signal.SIGXCPU, None, False)[:2][0], 504)
        self.assertEqual(server.child_outcome(-signal.SIGXCPU, None, False)[1]["error"]["code"], "timeout")

    def test_dead_child(self):
        for rc, out, note in ((-signal.SIGSEGV, None, "SIGSEGV"), (-signal.SIGKILL, None, "SIGKILL"),
                              (1, None, "exit 1"), (0, {"status": "x"}, "exit 0"), (0, ["no"], "exit 0"),
                              (0, {"status": 200}, "exit 0")):
            with self.subTest(rc=rc, out=out):
                status, payload, got_note = server.child_outcome(rc, out, False)
                self.assertEqual((status, payload["error"]["code"], got_note), (500, "internal", note))


class BodyLimits(unittest.TestCase):
    """SECURITY-03: the whole-body deadline and the byte budget of the bodies on /tmp (the HTTP side is
    test_api.SlowBodies)."""

    def test_body_seconds(self):
        self.assertEqual((settings.BODY_MIN_S, settings.BODY_RATE), (30, 256 * 1024))
        self.assertEqual(server.body_seconds(1), 30)
        self.assertEqual(server.body_seconds(30 * 256 * 1024), 30)
        self.assertEqual(server.body_seconds(30 * 256 * 1024 + 1), 31)
        self.assertEqual(server.body_seconds(settings.MAX_BYTES), 128)           # 32 MiB at 256 KiB/s

    def test_budget_default_is_one_plus_queue_bodies(self):
        self.assertEqual(settings.RECEIVE_BYTES, (1 + settings.QUEUE) * settings.MAX_BYTES)

    def test_budget(self):
        b = server.Budget(10)
        self.assertTrue(b.take(6))
        self.assertFalse(b.take(5))                     # refused whole: nothing taken
        self.assertEqual(b.used, 6)
        self.assertTrue(b.take(4))
        self.assertFalse(b.take(1))
        b.give(10)
        self.assertEqual(b.used, 0)
        self.assertTrue(b.take(10))


class Query(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(server.parse_query(""), {"pages": 3, "max_codes": 50, "pixel_limit": 50_000_000,
                                                  "formats": None})

    def test_values(self):
        self.assertEqual(server.parse_query("pages=10&max_codes=1&pixel_limit=1&formats=Aztec,QRCode"),
                         {"pages": 10, "max_codes": 1, "pixel_limit": 1, "formats": "Aztec,QRCode"})

    def test_refused(self):
        for q in ("pages=0", "pages=11", "max_codes=51", "pixel_limit=50000001", "pages=1e1", "pages=%2B3",
                  "formats=", "formats=x", "a=1", "pages=1&pages=1", "a=1&b=2&c=3&d=4&e=5"):
            with self.subTest(q=q):
                with self.assertRaises(server.ApiError) as e:
                    server.parse_query(q)
                self.assertEqual((e.exception.status, e.exception.code), (400, "bad_param"))


if __name__ == "__main__":
    unittest.main()
