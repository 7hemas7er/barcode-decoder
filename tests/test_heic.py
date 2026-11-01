"""HEIC/HEIF (plan §11 Q4, IBC-HEIC): accepted by magic bytes (an `ftyp` box with a HEIF image brand), decoded by
libheif through pi-heif (decode only), and refused with 422 too_many_pixels from the header (`ispe`) BEFORE any
decoding. A non-HEIF `ftyp` brand (AVIF, MP4, ...) is 415. The fixtures are fictional (tests/fixtures/aztec.heic and
barcode_decoder/selftest.heic: the Aztec code of support.BCBP), made by tests/fixtures/make_fixtures.py in a
throwaway container that has an encoder; the image has none (tests/host/test_container.py checks it).
"""
import os, random, struct, time, unittest
from unittest import mock

import support as s
from bcdecoder import engine, isobmff, server, worker

SELFTEST_HEIC = os.path.join(server.SELFTEST_DIR, "selftest.heic")


def box(kind, payload):
    return struct.pack(">I", 8 + len(payload)) + kind + payload


def full(kind, version, flags, payload):
    return box(kind, bytes([version]) + flags.to_bytes(3, "big") + payload)


def ispe(w, h):
    return full(b"ispe", 0, 0, struct.pack(">II", w, h))


def heif(props, assoc, primary=1, brand=b"heic", pitm_version=0, ipma_version=0, ipma_flags=0, mdat=b"\0" * 16):
    """A synthetic HEIF header: ftyp, meta (hdlr, pitm, iprp (ipco, ipma)), mdat. `assoc` maps item id -> list of
    1-based property indexes."""
    ftyp = box(b"ftyp", brand + b"\0\0\0\0" + b"mif1" + brand)
    hdlr = full(b"hdlr", 0, 0, b"\0\0\0\0pict" + b"\0" * 13)
    pitm = full(b"pitm", pitm_version, 0, primary.to_bytes(2 if pitm_version == 0 else 4, "big"))
    ipco = box(b"ipco", b"".join(props))
    entries = b""
    for item, indexes in assoc.items():
        entries += item.to_bytes(2 if ipma_version < 1 else 4, "big") + bytes([len(indexes)])
        for i in indexes:
            entries += (0x8000 | i).to_bytes(2, "big") if ipma_flags & 1 else bytes([0x80 | i])
    ipma = full(b"ipma", ipma_version, ipma_flags, struct.pack(">I", len(assoc)) + entries)
    meta = full(b"meta", 0, 0, hdlr + pitm + box(b"iprp", ipco + ipma))
    return ftyp + meta + box(b"mdat", mdat)


def patch_ispe(data, w, h):
    """The HEIC with every ispe property set to w x h (the rest untouched)."""
    b = bytearray(data)
    i = b.find(b"ispe")
    found = 0
    while i != -1:
        b[i + 8:i + 16] = struct.pack(">II", w, h)          # after type, version+flags
        found += 1
        i = b.find(b"ispe", i + 4)
    assert found, "no ispe"
    return bytes(b)


class Sniff(unittest.TestCase):
    def test_heif_brands_accepted(self):
        for brand in (b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1"):
            head = b"\0\0\0\x18ftyp" + brand + b"\0\0\0\0"
            self.assertEqual(engine.sniff(head), "heic", brand)

    def test_other_ftyp_brands_refused(self):
        for brand in (b"avif", b"avis", b"isom", b"iso2", b"mp41", b"mp42", b"M4A ", b"qt  ", b"crx ", b"3gp4",
                      b"HEIC", b"jpeg"):
            head = b"\0\0\0\x18ftyp" + brand + b"\0\0\0\0"
            self.assertIsNone(engine.sniff(head), brand)
        self.assertIsNone(engine.sniff(b"\0\0\0\x18xtypheic\0\0\0\0"))
        self.assertIsNone(engine.sniff(b"ftypheic"))                       # too short / wrong offset

    def test_fixture(self):
        self.assertEqual(engine.sniff(s.fixture("aztec.heic")), "heic")


class HeaderReader(unittest.TestCase):
    """isobmff.heif_sizes: bounded, never raises, never trusts a size."""

    def test_fixtures(self):
        self.assertEqual(isobmff.heif_sizes(s.fixture("aztec.heic")), ((800, 600), (800, 600)))
        with open(SELFTEST_HEIC, "rb") as f:
            self.assertEqual(isobmff.heif_sizes(f.read()), ((188, 188), (188, 188)))

    def test_primary_and_largest(self):
        data = heif([ispe(4032, 3024), ispe(512, 512), ispe(9000, 9000)], {1: [1], 2: [2], 3: [3]})
        self.assertEqual(isobmff.heif_sizes(data), ((4032, 3024), (9000, 9000)))
        data = heif([ispe(512, 512), ispe(4032, 3024)], {7: [1], 1: [2]}, primary=1)
        self.assertEqual(isobmff.heif_sizes(data), ((4032, 3024), (4032, 3024)))

    def test_wide_ids_and_indexes(self):
        data = heif([ispe(640, 480)], {70000: [1]}, primary=70000, pitm_version=1, ipma_version=1, ipma_flags=1)
        self.assertEqual(isobmff.heif_sizes(data), ((640, 480), (640, 480)))

    def test_largesize_box(self):
        data = heif([ispe(640, 480)], {1: [1]})
        meta_at = data.index(b"meta") - 4
        size = struct.unpack(">I", data[meta_at:meta_at + 4])[0]
        large = struct.pack(">I", 1) + b"meta" + struct.pack(">Q", size + 8) + data[meta_at + 8:meta_at + size]
        data = data[:meta_at] + large + data[meta_at + size:]
        self.assertEqual(isobmff.heif_sizes(data), ((640, 480), (640, 480)))

    def test_no_primary_association(self):
        self.assertEqual(isobmff.heif_sizes(heif([ispe(640, 480)], {2: [1]})), (None, (640, 480)))
        self.assertEqual(isobmff.heif_sizes(heif([], {1: []})), (None, None))

    def test_hostile_headers(self):
        good = heif([ispe(640, 480)], {1: [1]})
        cases = {
            "empty": b"",
            "ftyp only": good[:24],
            "size zero ftyp": b"\0\0\0\0ftypheic",
            "size below header": b"\0\0\0\x04ftypheic\0\0\0\0",
            "box past the end": struct.pack(">I", 1 << 30) + b"ftypheic\0\0\0\0",
            "largesize past the end": struct.pack(">I", 1) + b"ftyp" + struct.pack(">Q", 1 << 60) + b"heic",
            "ipma count 2^32-1": heif([ispe(640, 480)], {}).replace(b"ipma\0\0\0\0\0\0\0\0",
                                                                    b"ipma\0\0\0\0\xff\xff\xff\xff"),
            "ispe side 2^32-1": heif([ispe(0xFFFFFFFF, 1)], {1: [1]}),
            "ispe cut": heif([full(b"ispe", 0, 0, b"\0\0\x02")], {1: [1]}),
            "many boxes": good[:good.index(b"meta") - 4] + box(b"free", b"") * 6000 + good[good.index(b"meta") - 4:],
        }
        for name, data in cases.items():
            with self.subTest(name):
                start = time.monotonic()
                result = isobmff.heif_sizes(data)
                self.assertLess(time.monotonic() - start, 1)
                self.assertIsInstance(result, tuple)
                if name in ("ipma count 2^32-1",):
                    self.assertEqual(result, (None, None))

    def test_every_truncation_and_random_damage(self):
        data = s.fixture("aztec.heic")
        for n in range(0, len(data), 3):
            self.assertIsInstance(isobmff.heif_sizes(data[:n]), tuple)
        rnd = random.Random(20261031)
        start = time.monotonic()
        for _ in range(1500):
            b = bytearray(data[:400])                         # the header part: ftyp + meta
            for _ in range(rnd.randint(1, 8)):
                b[rnd.randrange(len(b))] = rnd.randrange(256)
            primary, largest = isobmff.heif_sizes(bytes(b))
            for size in (primary, largest):
                self.assertTrue(size is None or (len(size) == 2 and all(isinstance(v, int) for v in size)))
        self.assertLess(time.monotonic() - start, 20)


class GuardBeforeDecoding(unittest.TestCase):
    """In process: the worker answers too_many_pixels from the header, and libheif is never asked to decode."""

    def job(self, tmp, data, **k):
        with open(os.path.join(tmp, "in"), "wb") as f:
            f.write(data)
        return {"type": "heic", "pages": 3, "formats": None, "max_codes": 50, "pixel_limit": 50_000_000,
                "pdf_side": 2400, "text_max": 8192, "timeout_s": 60, **k}

    def test_huge_ispe(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            job = self.job(tmp, patch_ispe(s.fixture("aztec.heic"), 9000, 9000))
            with mock.patch.object(engine, "open_image", side_effect=AssertionError("decoded")), \
                    mock.patch.object(engine, "_register_heif", side_effect=AssertionError("libheif loaded")):
                with self.assertRaises(worker.JobError) as e:
                    worker.decode(tmp, job, time.monotonic())
        self.assertEqual((e.exception.status, e.exception.code, e.exception.extra),
                         (422, "too_many_pixels", {"width": 9000, "height": 9000}))

    def test_decodes_under_the_limit(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            body = worker.decode(tmp, self.job(tmp, s.fixture("aztec.heic")), time.monotonic())
        self.assertEqual((body["type"], body["width"], body["height"]), ("heic", 800, 600))
        self.assertEqual([b["text"] for b in body["barcodes"]], [s.BCBP])

    def test_engine_read_barcodes(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "photo.heic")
            with open(path, "wb") as f:
                f.write(s.fixture("aztec.heic"))
            self.assertEqual(engine.read_barcodes(path), [s.BCBP])
            with self.assertRaises(ValueError) as e:
                engine.read_barcodes(path, max_pixels=479_999)
            self.assertEqual(str(e.exception), "immagine di 800x600 pixel: troppo grande")


class HeicApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dec = s.DecoderProcess()
        cls.dec.__enter__()
        cls.heic = s.fixture("aztec.heic")

    @classmethod
    def tearDownClass(cls):
        cls.dec.__exit__(None, None, None)

    def test_decoded(self):
        status, body, _ = self.dec.decode(self.heic)
        self.assertEqual(status, 200, body)
        self.assertEqual((body["type"], body["width"], body["height"], body["pages"]), ("heic", 800, 600, None))
        self.assertEqual([(b["format"], b["text"]) for b in body["barcodes"]], [("Aztec", s.BCBP)])
        with open(SELFTEST_HEIC, "rb") as f:
            status, body, _ = self.dec.decode(f.read())
        self.assertEqual((status, s.texts(body)), (200, [s.BCBP]))

    def test_listed_in_health(self):
        self.assertIn("heic", self.dec.health()[1]["types"])

    def test_pixel_guard_before_decoding(self):
        for w, h in ((9000, 9000), (8000, 6251), (65535, 65535)):
            with self.subTest(size=(w, h)):
                start = time.monotonic()
                status, body, _ = self.dec.decode(patch_ispe(self.heic, w, h))
                took = time.monotonic() - start
                self.assertEqual((status, body["error"]["code"]), (422, "too_many_pixels"), body)
                self.assertEqual((body["error"]["width"], body["error"]["height"]), (w, h))
                self.assertLess(took, 1.5)

    def test_pixel_limit_parameter(self):
        status, body, _ = self.dec.decode(self.heic, "pixel_limit=479999")
        self.assertEqual((status, body["error"]["code"], body["error"]["width"], body["error"]["height"]),
                         (422, "too_many_pixels", 800, 600))
        self.assertEqual(self.dec.decode(self.heic, "pixel_limit=480000")[0], 200)

    def test_other_ftyp_brand_refused(self):
        for brand in (b"avif", b"isom", b"mp42"):
            with self.subTest(brand=brand):
                data = self.heic[:8] + brand + self.heic[12:]
                status, body, _ = self.dec.decode(data)
                self.assertEqual((status, body["error"]["code"]), (415, "unsupported_type"))

    def test_unreadable(self):
        no_ispe = self.heic.replace(b"ispe", b"xspe")             # the reader finds no size; libheif refuses it too
        for name, data in (("cut in the image data", self.heic[:len(self.heic) // 2]),
                           ("no ispe", no_ispe), ("header only", self.heic[:40])):
            with self.subTest(name):
                status, body, _ = self.dec.decode(data)
                self.assertEqual((status, body["error"]["code"]), (422, "unreadable"), body)


if __name__ == "__main__":
    unittest.main()
