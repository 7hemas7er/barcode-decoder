"""Makes the self-test files of the app and the HEIC fixtures of the tests. Fictional content only: the Aztec code
of support.BCBP (ROSSI/MARIO FR 1234 BLQ-STN).

It needs a HEIC ENCODER, which the decoder image must not contain (pi-heif there decodes only). So it runs in a
throwaway container, with pillow-heif (whose wheels bundle the x265 encoder) installed from
requirements-fixtures.txt; nothing of it reaches the image. From the repository root:

  docker run --rm --name bpt-wf-decoder-fixtures --user "$(id -u):$(id -g)" -e HOME=/tmp -v "$PWD:/src" -w /src \
    python:3.13-slim-trixie@sha256:2d44738ebf4ce5f63b9d9ea871e9c7cfdc0426f7b0c97cac915fd88ded1d86bb \
    sh -c 'pip install -q --user --only-binary=:all: --require-hashes --no-deps \
             -r tests/fixtures/requirements-fixtures.txt && python -B tests/fixtures/make_fixtures.py'

(as your own user, so the files are yours; -B but not -I, because the packages are in the user site of HOME=/tmp)

Writes (and checks by decoding them again):
  barcode_decoder/selftest.png   the Aztec code, PNG ("L")
  barcode_decoder/selftest.heic  the same, HEIC
  tests/fixtures/aztec.heic      a 800x600 "photo" with the code in it, HEIC
"""
import os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.dirname(HERE))

import pillow_heif  # noqa: E402
import zxingcpp  # noqa: E402
from PIL import Image  # noqa: E402

import support  # noqa: E402

pillow_heif.register_heif_opener()


def check(path, kind):
    with Image.open(path) as im:
        found = [r.text for r in zxingcpp.read_barcodes(im)]
    assert found == [support.BCBP], (path, found)
    print(f"{os.path.relpath(path, ROOT)}: {kind} {os.path.getsize(path)} bytes, decodes back")


def main():
    code = support.code_image(scale=4, border=40)
    png = os.path.join(ROOT, "barcode_decoder", "selftest.png")
    code.save(png, "PNG", optimize=True)
    check(png, "PNG")

    heic = os.path.join(ROOT, "barcode_decoder", "selftest.heic")
    code.convert("RGB").save(heic, "HEIF", quality=90)
    check(heic, "HEIC")

    photo = Image.new("RGB", (800, 600), (226, 228, 222))
    photo.paste(support.code_image(scale=5, border=24).convert("RGB"), (150, 90))
    fixture = os.path.join(HERE, "aztec.heic")
    photo.save(fixture, "HEIF", quality=85)
    check(fixture, "HEIC")


if __name__ == "__main__":
    main()
