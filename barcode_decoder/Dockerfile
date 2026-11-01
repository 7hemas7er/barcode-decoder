# barcode-decoder: one image for amd64 and arm64. The Home Assistant Supervisor builds it on the device from this
# file (the app's config.yaml has no `image` key); plain Docker users build it or pull the published image.
#
# The base is fixed here and pinned by the digest of its multi-arch index: build.yaml is deprecated and the
# Supervisor no longer passes BUILD_FROM. Debian (glibc), not the Alpine HA base images: zxing-cpp publishes
# manylinux wheels only.
FROM python:3.13-slim-trixie@sha256:2d44738ebf4ce5f63b9d9ea871e9c7cfdc0426f7b0c97cac915fd88ded1d86bb

LABEL org.opencontainers.image.title="barcode-decoder" \
      org.opencontainers.image.description="Reads the barcodes of images and PDFs over HTTP; stateless" \
      org.opencontainers.image.version="1.0.0" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/7hemas7er/barcode-decoder"

# pdftoppm renders PDF pages. poppler is GPL-2.0/GPL-3.0: it runs as a separate program (see THIRD_PARTY.md).
# gpg is named so that libgpgme's "gnupg | gpg" dependency takes the small gpg package, not the whole GnuPG suite
# (agent, dirmngr, pinentry); nothing here runs gpg.
RUN apt-get update \
 && apt-get install -y --no-install-recommends poppler-utils gpg \
 && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*.deb

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --disable-pip-version-check --root-user-action=ignore --only-binary=:all: --require-hashes --no-deps \
        -r /tmp/requirements.txt \
 && rm /tmp/requirements.txt \
 && python -I -B -c "import zxingcpp, PIL, pi_heif"

# The package and the self-test files in /app, made importable under -I by a .pth file; compiled now, because
# the root file system is read-only at run time and the processes run with -B
COPY bcdecoder/ /app/bcdecoder/
COPY selftest.png selftest.heic /app/
RUN python -I -B -c "import sysconfig; open(sysconfig.get_path('purelib') + '/barcode-decoder.pth', 'w').write('/app\n')" \
 && python -I -m compileall -q /app/bcdecoder \
 && useradd --uid 10001 --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin decoder \
 && chmod -R a+rX,go-w /app

# Nothing is written outside /tmp (a tmpfs: `tmpfs: true` in the app, --tmpfs /tmp with Docker)
ENV HOME=/tmp
WORKDIR /app
USER 10001
EXPOSE 8099
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
  CMD ["python", "-I", "-B", "-c", "import urllib.request as u; u.build_opener(u.ProxyHandler({})).open('http://127.0.0.1:8099/health', timeout=8)"]
CMD ["python", "-I", "-B", "-m", "bcdecoder.server"]
