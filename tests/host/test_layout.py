"""The repository layout of an HA app repository (plan §11 Q6) and the packaging rules: no other config.* file, the
app config of §5.9.4, the base image pinned by digest, the requirements pinned with hashes, the versions in step,
every shipped Python distribution in THIRD_PARTY.md.

  python3 -I -B -m unittest discover -s tests/host -t tests/host -v
"""
import os, re, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP = os.path.join(ROOT, "barcode_decoder")
# Supervisor 2026.10 (supervisor/apps/validate.py): the watchdog pattern and the slug rule
WATCHDOG = re.compile(r"^(?:https?|\[PROTO:\w+\]|tcp):\/\/\[HOST\]:(\[PORT:\d+\]|\d+).*$")
SLUG = re.compile(r"^[-_.A-Za-z0-9]+$")


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


class Layout(unittest.TestCase):
    def test_root(self):
        for name in ("repository.yaml", "README.md", "LICENSE", "THIRD_PARTY.md", "barcode_decoder", "tests"):
            self.assertTrue(os.path.exists(os.path.join(ROOT, name)), name)

    def test_app_folder(self):
        for name in ("config.yaml", "Dockerfile", "DOCS.md", "README.md", "CHANGELOG.md", "icon.png", "logo.png",
                     "requirements.txt", "selftest.png", "selftest.heic", "bcdecoder/__init__.py",
                     "bcdecoder/server.py", "bcdecoder/worker.py", "bcdecoder/engine.py", "bcdecoder/settings.py",
                     "bcdecoder/isobmff.py"):
            self.assertTrue(os.path.isfile(os.path.join(APP, name)), name)

    def test_no_other_config_file(self):
        """The Supervisor reads every config.yaml/.yml/.json of an app repository as an app (dot folders skipped)."""
        found = []
        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            found += [os.path.relpath(os.path.join(dirpath, f), ROOT) for f in filenames if f.startswith("config.")]
        self.assertEqual(found, [os.path.join("barcode_decoder", "config.yaml")])


class AppConfig(unittest.TestCase):
    def setUp(self):
        try:
            import yaml
        except ImportError:
            self.fail("PyYAML is needed for this test")
        self.config = yaml.safe_load(read("barcode_decoder", "config.yaml"))
        self.repository = yaml.safe_load(read("repository.yaml"))

    def test_keys(self):
        c = self.config
        self.assertEqual((c["name"], c["version"], c["slug"]), ("Barcode Decoder", "1.0.0", "barcode_decoder"))
        self.assertEqual(c["description"], "Legge i codici a barre di foto e PDF (nessun dato salvato)")
        self.assertEqual(sorted(c["arch"]), ["aarch64", "amd64"])
        self.assertEqual((c["startup"], c["boot"], c["init"], c["tmpfs"]), ("application", "auto", True, True))
        self.assertEqual(c["ports"], {"8099/tcp": None})
        self.assertEqual(c["ports_description"], {"8099/tcp": "API per Home Assistant (non serve aprirla)"})
        self.assertEqual(c["watchdog"], "http://[HOST]:[PORT:8099]/health")
        self.assertRegex(c["watchdog"], WATCHDOG)
        self.assertRegex(c["slug"], SLUG)
        self.assertNotIn("image", c)                     # the Supervisor builds the Dockerfile on the device
        self.assertFalse({"ingress", "hassio_api", "homeassistant_api", "auth_api", "map", "host_network"} & set(c))

    def test_repository(self):
        self.assertEqual(self.repository["name"], "Barcode Decoder")
        self.assertEqual(self.repository["url"], "https://github.com/7hemas7er/barcode-decoder")

    def test_versions_in_step(self):
        version = self.config["version"]
        init = read("barcode_decoder", "bcdecoder", "__init__.py")
        self.assertIn(f'__version__ = "{version}"', init)
        self.assertIn(f'org.opencontainers.image.version="{version}"', read("barcode_decoder", "Dockerfile"))
        self.assertRegex(read("barcode_decoder", "CHANGELOG.md"), rf"(?m)^## {re.escape(version)}\b")


class Packaging(unittest.TestCase):
    def test_dockerfile(self):
        d = read("barcode_decoder", "Dockerfile")
        (base,) = re.findall(r"(?m)^FROM (\S+)", d)
        self.assertRegex(base, r"^python:3\.13-slim-trixie@sha256:[0-9a-f]{64}$")
        self.assertRegex(d, r"(?m)^USER 10001$")
        self.assertRegex(d, r"(?m)^EXPOSE 8099$")
        self.assertRegex(d, r"(?m)^HEALTHCHECK .*\n\s+CMD \[\"python\", \"-I\", \"-B\", \"-c\"")
        self.assertRegex(d, r'(?m)^CMD \["python", "-I", "-B", "-m", "bcdecoder.server"\]$')
        self.assertIn("--require-hashes", d)
        self.assertIn("--only-binary=:all:", d)
        self.assertNotIn("ARG BUILD_FROM", d)

    def test_requirements_pinned_with_hashes(self):
        text = read("barcode_decoder", "requirements.txt")
        reqs = re.findall(r"(?m)^([A-Za-z0-9_.-]+)==([0-9][^ \\\n]*)", text)
        self.assertEqual(sorted(n.lower() for n, _ in reqs), ["pi-heif", "pillow", "zxing-cpp"])
        self.assertIn(("zxing-cpp", "3.1.1"), reqs)
        blocks = re.split(r"(?m)^(?=[A-Za-z0-9_.-]+==)", text)[1:]
        for block in blocks:
            hashes = re.findall(r"--hash=sha256:([0-9a-f]{64})", block)
            self.assertEqual(len(hashes), 2, block)           # one wheel for aarch64, one for x86_64
        self.assertNotRegex(text, r"(?m)^pillow-heif")         # its wheels bundle x265 (GPL-2.0)

    def test_third_party_lists_every_shipped_component(self):
        text = read("THIRD_PARTY.md")
        for name in ("zxing-cpp", "Pillow", "pi-heif", "libheif", "libde265", "poppler", "Python",
                     "python:3.13-slim-trixie", "GPL"):
            self.assertIn(name, text)
        for package in re.findall(r"(?m)^\| `([a-z0-9][a-z0-9.+-]+)` \|", text):
            self.assertNotIn(" ", package)

    def test_selftest_files_are_fictional(self):
        worker = read("barcode_decoder", "bcdecoder", "worker.py")
        self.assertFalse("ROSSI" in worker, "worker.py names the fictional passenger: it should hold a sha256 only")


if __name__ == "__main__":
    unittest.main()
