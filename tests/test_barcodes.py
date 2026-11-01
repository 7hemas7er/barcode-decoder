"""The 8 ReadBarcodes and RealAztec tests of boarding-pass-tracker's tracker/test_barcodes.py, moved here with
engine.py (the decode and the PDF render of tracker/barcodes.py). Adapted, because the zbar fallback is gone:
- the import lines (engine as bc, support as tf);
- NO_DECODER is the engine's message (no zbarimg in it);
- the fake zbarimg is replaced by the fake zxingcpp module where a test needed "a decoder that finds the BCBP":
  test_zbarimg became test_pdf_through_pdftoppm (same checks: the pages go through pdftoppm, and their temporary
  folder, now named bcdecoder-pages-*, is removed); test_errors_never_reach_the_caller and
  test_pdf_past_the_timeout use the fake zxingcpp;
- WithDecoders no longer hides zbarimg from PATH.
The TooLarge tests stay in the integration (the pixel guard runs there, before the decoder is called).

  python -I -B -m unittest discover -s tests -t tests -p 'test_barcodes.py' -v       (inside the image)
"""
import contextlib, io, os, shutil, sys, tempfile, time, types, unittest
from unittest import mock

from bcdecoder import engine as bc
import support as tf  # the synthetic BCBP
from PIL import Image

BCBP = tf.bcbp()                    # fictional: ROSSI/MARIO, FR 1234 BLQ-STN
NO_DECODER = "zxing-cpp is not installed: barcodes in photos and PDFs cannot be read"
PDFTOPPM = shutil.which("pdftoppm")


class WithDecoders(unittest.TestCase):
    """No real decoder unless a test puts one back: zxingcpp hidden."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="bcdecoder-test-barcodes-")
        self._path, self._dec = os.environ.get("PATH", ""), bc._decoder
        self._zx = sys.modules.get("zxingcpp", "absent")
        sys.modules["zxingcpp"] = None                     # import zxingcpp -> ImportError
        bc._decoder = None
        self.png = os.path.join(self.dir, "photo.png")
        Image.new("RGB", (16, 16), "white").save(self.png)
        self.pdf = os.path.join(self.dir, "pass.pdf")
        Image.new("RGB", (64, 64), "white").save(self.pdf, "PDF")

    def tearDown(self):
        os.environ["PATH"], bc._decoder = self._path, self._dec
        if self._zx == "absent":
            sys.modules.pop("zxingcpp", None)
        else:
            sys.modules["zxingcpp"] = self._zx
        shutil.rmtree(self.dir, True)

    def fake_bin(self, name, script):
        bin_ = os.path.join(self.dir, "bin")
        os.makedirs(bin_, exist_ok=True)
        with open(os.path.join(bin_, name), "w") as f:
            f.write("#!/bin/sh\n" + script)
        os.chmod(os.path.join(bin_, name), 0o755)
        os.environ["PATH"] = bin_ + os.pathsep + self._path

    def fake_zxingcpp(self, texts):
        calls = []
        fake = types.ModuleType("zxingcpp")
        fake.read_barcodes = lambda im: calls.append(im.size) or [types.SimpleNamespace(text=t) for t in texts]
        sys.modules["zxingcpp"] = fake
        return calls

    def write(self, name, data):
        path = os.path.join(self.dir, name)
        with open(path, "wb") as f:
            f.write(data)
        return path


class ReadBarcodes(WithDecoders):
    def test_no_decoder_a_single_warning(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(bc.read_barcodes(self.png), [])
            self.assertEqual(bc.read_barcodes(self.pdf), [])
        (line,) = out.getvalue().splitlines()
        self.assertTrue(line.endswith("barcodes: " + NO_DECODER), line)

    @unittest.skipUnless(PDFTOPPM, "poppler pdftoppm is not installed")
    def test_pdf_through_pdftoppm(self):                            # was test_zbarimg
        calls = self.fake_zxingcpp([BCBP])
        self.assertEqual(bc.read_barcodes(self.png), [BCBP])
        made, real = [], tempfile.TemporaryDirectory

        def spy(**k):
            made.append(real(**k))
            return made[-1]
        with mock.patch.object(bc.tempfile, "TemporaryDirectory", spy):
            self.assertEqual(bc.read_barcodes(self.pdf), [BCBP])   # through pdftoppm: zxing receives PNG pages
        (td,) = made
        self.assertTrue(os.path.basename(td.name).startswith("bcdecoder-pages-"), td.name)
        self.assertFalse(os.path.exists(td.name))                   # the pages are removed
        self.assertEqual(len(calls), 2)
        self.assertEqual(max(calls[1]), bc.PDF_SIDE)                # the page, 2400 px on the long side

    def test_errors_never_reach_the_caller(self):
        self.fake_zxingcpp([BCBP])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(bc.read_barcodes(os.path.join(self.dir, "missing.png")), [])
            self.assertEqual(bc.read_barcodes(self.write("broken.pdf", b"%PDF-1.7 broken")), [])
            self.assertEqual(bc.read_barcodes(self.write("text.png", b"not an image")), [])

    def test_zxingcpp(self):
        calls = self.fake_zxingcpp([BCBP, BCBP, "  ", "x" * 600])
        self.assertEqual(bc.read_barcodes(self.png), [BCBP, "x" * 500])     # no duplicates, no blanks, 500 chars
        self.assertEqual(calls, [(16, 16)])

    def test_at_most_max_codes(self):
        self.fake_zxingcpp([f"code {i}" for i in range(bc.MAX_CODES + 5)])
        self.assertEqual(bc.read_barcodes(self.png), [f"code {i}" for i in range(bc.MAX_CODES)])

    def test_pdf_past_the_timeout(self):
        self.fake_zxingcpp([BCBP])
        self.fake_bin("pdftoppm", "exec sleep 30\n")      # exec: the timeout kills sleep itself
        out, start = io.StringIO(), time.monotonic()
        with mock.patch.object(bc, "PDF_TIMEOUT", 0.5), contextlib.redirect_stdout(out):
            self.assertEqual(bc.read_barcodes(self.pdf), [])
        self.assertLess(time.monotonic() - start, 10)
        self.assertIn("barcodes not read (TimeoutExpired)", out.getvalue())


class RealAztec(unittest.TestCase):
    """A real Aztec code, the barcode of most boarding passes, written and read by zxing-cpp."""

    def setUp(self):
        try:
            import zxingcpp
        except ImportError:
            self.skipTest("zxing-cpp is not installed")
        if hasattr(zxingcpp, "create_barcode"):
            image = zxingcpp.write_barcode_to_image(zxingcpp.create_barcode(BCBP, zxingcpp.BarcodeFormat.Aztec),
                                                    scale=4)
        elif hasattr(zxingcpp, "write_barcode"):
            image = zxingcpp.write_barcode(zxingcpp.BarcodeFormat.Aztec, BCBP, 400, 400)
        else:
            self.skipTest("this zxing-cpp cannot write barcodes")
        view = memoryview(image)
        self.dir = tempfile.mkdtemp(prefix="bcdecoder-test-barcodes-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.photo = Image.frombuffer("L", (view.shape[1], view.shape[0]), bytes(view), "raw", "L", 0, 1)
        self._dec, bc._decoder = bc._decoder, None
        self.addCleanup(setattr, bc, "_decoder", self._dec)

    def test_photo(self):
        path = os.path.join(self.dir, "pass.png")
        self.photo.save(path)
        self.assertEqual(bc.read_barcodes(path), [BCBP])
        self.assertEqual(bc._decoder, "zxingcpp")

    @unittest.skipUnless(PDFTOPPM, "poppler pdftoppm is not installed")
    def test_pdf(self):
        path = os.path.join(self.dir, "pass.pdf")
        page = Image.new("L", (1240, 1754), 255)                     # A4 at 150 dpi, the code in a corner
        page.paste(self.photo, (100, 100))
        page.save(path, "PDF", resolution=150)
        self.assertEqual(bc.read_barcodes(path), [BCBP])


if __name__ == "__main__":
    unittest.main()
