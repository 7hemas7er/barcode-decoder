"""T-D11 on the host, with Docker: the image runs read-only, as UID 10001, with no capabilities, /tmp a tmpfs, as
in the documented `docker run` (plan §5.9.4); /health and the self-test answer; the image holds no HEIC encoder.

  BCDECODER_IMAGE=local/barcode-decoder:1.0.0 python3 -I -B -m unittest discover -s tests/host -t tests/host -v

Containers are named "$BCDECODER_CONTAINER_PREFIX-..." (default barcode-decoder-test) and removed at the end. They
publish on 127.0.0.1 only, on a free port.
"""
import json, os, socket, subprocess, time, unittest, urllib.error, urllib.request

IMAGE = os.environ.get("BCDECODER_IMAGE", "local/barcode-decoder:1.0.0")
PREFIX = os.environ.get("BCDECODER_CONTAINER_PREFIX", "barcode-decoder-test")
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SECRETS = ("M1ROSSI/MARIO", "ROSSI", "MARIO", "XYZ789")
BCBP = "M1ROSSI/MARIO MR      EXYZ789 BLQSTNFR 1234 310Y012A0042 100"     # tests/support.py: bcbp()
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def docker(*args, check=True, timeout=120):
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check, timeout=timeout)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def call(port, path, data=None, timeout=60):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method="POST" if data else "GET",
                                 headers={"Content-Type": "application/octet-stream"} if data else {})
    try:
        with OPENER.open(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


@unittest.skipUnless(os.environ.get("BCDECODER_IMAGE"), "set BCDECODER_IMAGE to run the container tests")
class Container(unittest.TestCase):
    """The documented plain-Docker run: --read-only --tmpfs /tmp --memory 1500m --pids-limit 64 --cap-drop ALL
    --security-opt no-new-privileges -p 127.0.0.1:<port>:8099 (no --user: the image's USER 10001)."""

    @classmethod
    def setUpClass(cls):
        cls.name = f"{PREFIX}-t11-{os.getpid()}"
        cls.port = free_port()
        docker("run", "-d", "--name", cls.name, "--read-only", "--tmpfs", "/tmp:size=256m", "--memory", "1500m",
               "--pids-limit", "64", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
               "-p", f"127.0.0.1:{cls.port}:8099", IMAGE)
        deadline = time.monotonic() + 30
        while True:
            try:
                if call(cls.port, "/health", timeout=2)[0] == 200:
                    break
            except OSError:
                pass
            if time.monotonic() > deadline:
                logs = docker("logs", cls.name, check=False).stdout
                docker("rm", "-f", cls.name, check=False)
                raise RuntimeError("the container did not answer: " + logs)
            time.sleep(0.3)

    @classmethod
    def tearDownClass(cls):
        docker("rm", "-f", cls.name, check=False)

    def exec(self, *cmd, check=False):
        return docker("exec", self.name, *cmd, check=check)

    def test_health_and_selftest(self):
        status, body = call(self.port, "/health")
        self.assertEqual((status, body["ok"], body["api"], body["version"]), (200, True, 1, "1.0.0"))
        self.assertEqual(body["types"], ["jpeg", "png", "webp", "pdf", "heic"])
        status, body = call(self.port, "/health?selftest=1")
        self.assertEqual((status, body["selftest"]), (200, "ok"), body)
        self.assertEqual(body["selftest_checks"], {"png": "ok", "pdf": "ok", "heic": "ok"})

    def test_decodes(self):
        for path in ("barcode_decoder/selftest.png", "barcode_decoder/selftest.heic", "tests/fixtures/aztec.heic"):
            with self.subTest(path), open(os.path.join(ROOT, path), "rb") as f:
                status, body = call(self.port, "/v1/decode", f.read())
            self.assertEqual((status, [b["text"] for b in body["barcodes"]]), (200, [BCBP]), body)

    def test_non_root_read_only_no_capabilities(self):
        info = json.loads(docker("inspect", self.name).stdout)[0]
        self.assertEqual(info["Config"]["User"], "10001")
        self.assertTrue(info["HostConfig"]["ReadonlyRootfs"])
        self.assertEqual(info["HostConfig"]["CapDrop"], ["ALL"])
        self.assertIn("no-new-privileges", info["HostConfig"]["SecurityOpt"])
        self.assertEqual(info["HostConfig"]["Tmpfs"], {"/tmp": "size=256m"})
        self.assertEqual(self.exec("id", "-u").stdout.strip(), "10001")
        status = self.exec("cat", "/proc/1/status").stdout
        fields = dict(line.split(":", 1) for line in status.splitlines() if ":" in line)
        self.assertEqual(fields["Uid"].split()[:2], ["10001", "10001"])
        self.assertEqual(fields["CapEff"].strip(), "0000000000000000")
        self.assertEqual(fields["NoNewPrivs"].strip(), "1")
        self.assertIn("bcdecoder.server", self.exec("cat", "/proc/1/cmdline").stdout)
        for path in ("/app/x", "/x", "/usr/local/lib/x", "/nonexistent"):
            r = self.exec("sh", "-c", f"echo x > {path}")
            self.assertNotEqual(r.returncode, 0, path)
            self.assertRegex(r.stderr, "Read-only file system|Permission denied")
        self.assertEqual(self.exec("sh", "-c", "echo x > /tmp/probe && rm /tmp/probe").returncode, 0)
        mounts = self.exec("cat", "/proc/mounts").stdout
        root = [line for line in mounts.splitlines() if line.split()[1] == "/"][0]
        self.assertIn("ro", root.split()[3].split(","))
        tmp = [line for line in mounts.splitlines() if line.split()[1] == "/tmp"][0]
        self.assertTrue(tmp.startswith("tmpfs /tmp tmpfs"))
        self.assertIn("noexec", tmp)

    def test_job_folder_removed(self):
        with open(os.path.join(ROOT, "barcode_decoder/selftest.png"), "rb") as f:
            self.assertEqual(call(self.port, "/v1/decode", f.read())[0], 200)
        self.assertEqual(self.exec("sh", "-c", "ls -A /tmp").stdout.strip(), "")

    def test_slow_senders_hold_no_job_slot(self):
        """SECURITY-03 in the documented run: five clients that send a Content-Length and then a byte a second (one
        per job slot) do not stop a real decode, and /tmp is empty once they are gone."""
        socks = []
        try:
            for _ in range(5):
                sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
                sock.sendall(b"POST /v1/decode HTTP/1.0\r\nContent-Length: 100000\r\n\r\n\x89")
                socks.append(sock)
            for _ in range(2):
                time.sleep(1)
                for sock in socks:
                    sock.sendall(b"P")
            with open(os.path.join(ROOT, "barcode_decoder/selftest.png"), "rb") as f:
                status, body = call(self.port, "/v1/decode", f.read(), timeout=20)
            self.assertEqual((status, [b["text"] for b in body["barcodes"]]), (200, [BCBP]), body)
        finally:
            for sock in socks:
                sock.close()
        deadline = time.monotonic() + 10
        while self.exec("sh", "-c", "ls -A /tmp").stdout.strip() and time.monotonic() < deadline:
            time.sleep(0.2)
        self.assertEqual(self.exec("sh", "-c", "ls -A /tmp").stdout.strip(), "")

    def test_docker_healthcheck_command(self):
        cmd = json.loads(docker("inspect", "--format", "{{json .Config.Healthcheck.Test}}", IMAGE).stdout)
        self.assertEqual(cmd[0], "CMD")
        self.assertEqual(self.exec(*cmd[1:]).returncode, 0)

    def test_zz_log_holds_no_decoded_text(self):
        logs = docker("logs", self.name).stdout + docker("logs", self.name).stderr
        self.assertIn("listening on 0.0.0.0:8099", logs)
        self.assertIn("decode type=heic", logs)
        for secret in SECRETS:
            self.assertNotIn(secret, logs)


@unittest.skipUnless(os.environ.get("BCDECODER_IMAGE"), "set BCDECODER_IMAGE to run the container tests")
class Image(unittest.TestCase):
    def run_in_image(self, *cmd, user=None):
        extra = ["--user", user] if user else []
        return docker("run", "--rm", "--name", f"{PREFIX}-img-{os.getpid()}", "--read-only", "--network", "none",
                      *extra, IMAGE, *cmd)

    def test_no_heic_encoder_and_no_gpl_python_wheel(self):
        r = self.run_in_image("python", "-I", "-B", "-c",
                              "import json, pi_heif, importlib.metadata as m;"
                              "print(json.dumps({'info': pi_heif.libheif_info(),"
                              " 'dists': sorted(d.metadata['Name'].lower().replace('_', '-')"
                              "                 for d in m.distributions())}))")
        out = json.loads(r.stdout)
        self.assertEqual(out["info"]["encoders"], {"mask": "mask"})          # libheif's built-in mask codec only
        self.assertEqual(list(out["info"]["decoders"]), ["libde265"])
        self.assertNotIn("pillow-heif", out["dists"])
        self.assertTrue({"zxing-cpp", "pillow", "pi-heif"} <= set(out["dists"]))
        # as root, so that no directory is unreadable to the search (read-only, no network, nothing mounted)
        found = self.run_in_image("find", "/", "-xdev", "(", "-iname", "*x265*", "-o", "-iname", "*kvazaar*", "-o",
                                  "-iname", "*libaom*", "-o", "-iname", "*rav1e*", "-o", "-iname", "*openh264*",
                                  ")", "-print", user="0")
        self.assertEqual((found.returncode, found.stdout.strip()), (0, ""))

    def test_third_party_lists_every_added_package(self):
        """Every Debian package the Dockerfile adds to the pinned base, and every Python distribution, is in
        THIRD_PARTY.md."""
        import re
        with open(os.path.join(ROOT, "barcode_decoder", "Dockerfile")) as f:
            (base,) = re.findall(r"(?m)^FROM (\S+)", f.read())
        query = ["dpkg-query", "-W", "-f", "${db:Status-Abbrev}|${Package}\n"]
        listed = lambda out: {line.split("|")[1] for line in out.splitlines() if line.startswith("ii")}  # noqa: E731
        in_base = listed(docker("run", "--rm", "--name", f"{PREFIX}-base-{os.getpid()}", "--network", "none", base,
                                *query).stdout)
        in_image = listed(docker("run", "--rm", "--name", f"{PREFIX}-pkgs-{os.getpid()}", "--network", "none",
                                 "--entrypoint", query[0], IMAGE, *query[1:]).stdout)
        with open(os.path.join(ROOT, "THIRD_PARTY.md")) as f:
            text = f.read()
        documented = set(re.findall(r"(?m)^\| `([a-z0-9][a-z0-9.+-]+)` \|", text))
        added = in_image - in_base
        self.assertGreater(len(added), 0)
        self.assertEqual(sorted(added - documented), [])
        self.assertEqual(sorted(documented - added - {"zxing-cpp", "pi-heif", "Pillow"}), [])
        for dist in ("zxing-cpp", "Pillow", "pi-heif", "pip"):
            self.assertIn(dist, text)

    def test_d7_as_pid_1_the_killed_job_leaves_no_process(self):
        """The documented run has no --init: the server is PID 1, so the orphans of a killed job (here a fake
        pdftoppm that leaves a background child) become its children. After the 504 none is left, not even as a
        zombie, and the next request works."""
        name, port = f"{PREFIX}-pid1-{os.getpid()}", free_port()
        fake = os.path.join(ROOT, "tests", "fakebin", "stuck")
        docker("run", "-d", "--name", name, "--read-only", "--tmpfs", "/tmp:size=64m", "--cap-drop", "ALL",
               "--security-opt", "no-new-privileges", "-e", "BCDECODER_TIMEOUT_S=3", "-e",
               "PATH=/fake:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "-v", f"{fake}:/fake:ro",
               "-p", f"127.0.0.1:{port}:8099", IMAGE)
        try:
            deadline = time.monotonic() + 30
            while True:
                try:
                    if call(port, "/health", timeout=2)[0] == 200:
                        break
                except OSError:
                    if time.monotonic() > deadline:
                        raise
                time.sleep(0.3)
            with open(os.path.join(ROOT, "barcode_decoder", "selftest.png"), "rb") as f:
                png = f.read()
            pdf = b"%PDF-1.4\n" + b"%" * 64 + b"\n%%EOF\n"          # never parsed: the fake pdftoppm just hangs
            status, body = call(port, "/v1/decode", pdf)
            self.assertEqual((status, body["error"]["code"]), (504, "timeout"))
            ps = "for d in /proc/[0-9]*; do tr '\\0' ' ' < $d/cmdline; echo \"|$(cut -d' ' -f3 $d/stat)\"; done"
            left = []
            for _ in range(20):
                out = docker("exec", name, "sh", "-c", ps, check=False).stdout
                left = [line for line in out.splitlines() if line.startswith("sleep") or line.endswith("|Z")]
                if not left:
                    break
                time.sleep(0.25)
            self.assertEqual(left, [])
            self.assertEqual(docker("exec", name, "cat", "/proc/1/cmdline").stdout.split("\0")[:5],
                             ["python", "-I", "-B", "-m", "bcdecoder.server"])
            status, body = call(port, "/v1/decode", png)
            self.assertEqual((status, [b["text"] for b in body["barcodes"]]), (200, [BCBP]))
        finally:
            docker("rm", "-f", name, check=False)

    def test_stops_quickly_as_pid_1(self):
        name = f"{PREFIX}-stop-{os.getpid()}"
        docker("run", "-d", "--name", name, "--read-only", "--tmpfs", "/tmp", "--network", "none", IMAGE)
        try:
            time.sleep(1.5)
            start = time.monotonic()
            docker("stop", "-t", "20", name)
            self.assertLess(time.monotonic() - start, 5)          # SIGTERM handled: no 20 s wait for SIGKILL
            self.assertIn("stopped", docker("logs", name).stdout)
        finally:
            docker("rm", "-f", name, check=False)


if __name__ == "__main__":
    unittest.main()
