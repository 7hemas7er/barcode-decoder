# Third-party components

What the `barcode-decoder` image ships, and under which licences. The decoder's own code (`barcode_decoder/bcdecoder`,
the tests, the self-test images) is MIT: see [LICENSE](LICENSE).

Checked on the image built from `barcode_decoder/Dockerfile` on 31/10/2026 (arm64; the amd64 build installs the
same package versions and the x86_64 wheels pinned in `requirements.txt`).

## GPL components (flagged)

| Component | Licence | Why it is there | How it is used |
|---|---|---|---|
| poppler: `poppler-utils`, `libpoppler147` | GPL-2.0-only or GPL-3.0-only | `pdftoppm` renders the pages of a PDF | run as a separate program on the job's file (a subprocess); never linked with the decoder's code |
| `libjbig0` | GPL-2.0-or-later | a dependency of libtiff, which poppler needs | loaded by poppler only |
| `gpg`, `gpgconf` | GPL-3.0-or-later | a dependency of libgpgme (poppler's signature support) | never run |
| The Debian base: `bash`, `coreutils`, `dpkg`, `apt`, `tar`, `sed`, `grep`, `findutils`, `gzip`, `diffutils`; `util-linux` (mixed) | GPL-2.0/GPL-3.0 | the base image's userland | not used by the decoder at run time (the `HEALTHCHECK` runs `python`, not a shell); `sh` is there for `docker exec` |

**The HEIC path has no GPL component.** pi-heif's wheels bundle libheif and libde265 (LGPL-3.0) and **no encoder**
(`pi_heif.libheif_info()`: decoders `libde265`, encoders only libheif's built-in `mask`). pillow-heif is not used:
its wheels bundle the x265 HEVC encoder (GPL-2.0). The HEIC test fixtures were written with pillow-heif in a
throwaway container (`tests/fixtures/make_fixtures.py`); only the resulting data files are in the repository, and
nothing of the encoder is in the image (`tests/host/test_container.py` checks it).

## Base image

`python:3.13-slim-trixie@sha256:2d44738ebf4ce5f63b9d9ea871e9c7cfdc0426f7b0c97cac915fd88ded1d86bb` (the multi-arch
index; Debian 13.7 "trixie" slim, 87 Debian packages):

| Component | Version | Licence |
|---|---|---|
| Python (built by the official image) | 3.13.18 | PSF-2.0 |
| pip (and the packages it vendors) | 26.2.2 | MIT (vendored: MIT, Apache-2.0, BSD, MPL-2.0) |
| Debian base packages (libc6, libssl3t64, libsqlite3-0, ...) | Debian 13.7 | each package's `/usr/share/doc/<package>/copyright` in the image |

## Python wheels (`barcode_decoder/requirements.txt`, pinned with hashes)

| Distribution | Version | Licence | Bundled in the wheel |
|---|---|---|---|
| `zxing-cpp` | 3.1.1 | Apache-2.0 | the zxing-cpp C++ library, statically built, with its nanobind binding (BSD-3-Clause); links only to glibc and libstdc++ |
| `Pillow` | 13.0.0 | MIT-CMU | in `pillow.libs`: libjpeg-turbo (IJG, BSD-3-Clause, Zlib), libpng (libpng-2.0), zlib (Zlib), bzip2 (bzip2-1.0.6), libtiff (libtiff), libwebp + libsharpyuv (BSD-3-Clause), libavif (BSD-2-Clause) with dav1d (BSD-2-Clause) and libaom (BSD-2-Clause + AOM patent licence), libyuv (BSD-3-Clause), OpenJPEG (BSD-2-Clause), FreeType (FTL, chosen from its FTL / GPL-2.0 dual licence), HarfBuzz (MIT), raqm (MIT), Little CMS (MIT), liblzma (0BSD), zstd (BSD-3-Clause), brotli (MIT), libxcb and libXau (MIT/X11). The full texts are in the wheel's `LICENSE` |
| `pi-heif` | 1.4.0 | BSD-3-Clause (the binding); the wheel as a whole LGPL-3.0 | in `pi_heif.libs`, as shared libraries: **libheif 1.23.0** (LGPL-3.0) and **libde265 1.1.0** (LGPL-3.0). The wheel's `LICENSES_bundled.txt` still links the sources of libheif 1.18.1 and libde265 1.0.15; the versions actually shipped are the ones above (soname `libheif.so.1.23.0`, `libheif_info()`) |

The LGPL libraries are shipped unmodified, as shared libraries that can be replaced. Their sources:
<https://github.com/strukturag/libheif/tree/v1.23.0> and <https://github.com/strukturag/libde265/tree/v1.1.0>;
pi-heif: <https://github.com/bigcat88/pillow_heif> (the `pi-heif` build of pillow_heif).

## Debian packages added by the Dockerfile

`apt-get install --no-install-recommends poppler-utils gpg` adds these 63 packages to the base (the licence of the
binary package's library or program; the full copyright files are in the image under `/usr/share/doc/<package>/`,
and the sources at <https://sources.debian.org/>):

| Package | Version | Source | Licence | GPL |
|---|---|---|---|---|
| `fontconfig-config` | 2.15.0-2.3 | fontconfig | HPND-style (fontconfig) |  |
| `fonts-dejavu-core` | 2.37-8 | fonts-dejavu | Bitstream-Vera; DejaVu changes public domain |  |
| `fonts-dejavu-mono` | 2.37-8 | fonts-dejavu | Bitstream-Vera; DejaVu changes public domain |  |
| `gpg` | 2.4.7-21+deb13u1+b5 | gnupg2 | GPL-3.0-or-later | **yes** |
| `gpgconf` | 2.4.7-21+deb13u1+b5 | gnupg2 | GPL-3.0-or-later | **yes** |
| `libassuan9` | 3.0.2-2 | libassuan | LGPL-2.1-or-later |  |
| `libbrotli1` | 1.1.0-2+deb13u1 | brotli | MIT |  |
| `libcairo2` | 1.18.4-1+b1 | cairo | LGPL-2.1 or MPL-1.1 |  |
| `libcom-err2` | 1.47.2-3+b12 | e2fsprogs | MIT (com_err) |  |
| `libcurl3t64-gnutls` | 8.14.1-2+deb13u7 | curl | curl (MIT-style) |  |
| `libdeflate0` | 1.23-2 | libdeflate | MIT |  |
| `libexpat1` | 2.9.0-1~deb13u1 | expat | MIT |  |
| `libfontconfig1` | 2.15.0-2.3 | fontconfig | HPND-style (fontconfig) |  |
| `libfreetype6` | 2.13.3+dfsg-1+deb13u1 | freetype | FTL or GPL-2.0-or-later (dual) |  |
| `libgcrypt20` | 1.11.0-7+deb13u1 | libgcrypt20 | LGPL-2.1-or-later |  |
| `libgnutls30t64` | 3.8.9-3+deb13u4 | gnutls28 | LGPL-2.1-or-later |  |
| `libgpg-error0` | 1.51-4 | libgpg-error | LGPL-2.1-or-later |  |
| `libgpgme11t64` | 1.24.2-3 | gpgme1.0 | LGPL-2.1-or-later |  |
| `libgpgmepp6t64` | 1.24.2-3 | gpgme1.0 | LGPL-2.1-or-later |  |
| `libgssapi-krb5-2` | 1.21.3-5+deb13u1 | krb5 | MIT (Kerberos) |  |
| `libidn2-0` | 2.3.8-2 | libidn2 | LGPL-3.0-or-later or GPL-2.0-or-later |  |
| `libjbig0` | 2.1-6.1+b2 | jbigkit | GPL-2.0-or-later | **yes** |
| `libjpeg62-turbo` | 1:2.1.5-4 | libjpeg-turbo | IJG, BSD-3-Clause, Zlib |  |
| `libk5crypto3` | 1.21.3-5+deb13u1 | krb5 | MIT (Kerberos) |  |
| `libkeyutils1` | 1.6.3-6 | keyutils | LGPL-2.1-or-later |  |
| `libkrb5-3` | 1.21.3-5+deb13u1 | krb5 | MIT (Kerberos) |  |
| `libkrb5support0` | 1.21.3-5+deb13u1 | krb5 | MIT (Kerberos) |  |
| `libksba8` | 1.6.7-2+b1 | libksba | LGPL-3.0-or-later or GPL-2.0-or-later |  |
| `liblcms2-2` | 2.16-2+deb13u2 | lcms2 | MIT |  |
| `libldap2` | 2.6.10+dfsg-1 | openldap | OpenLDAP-2.8 |  |
| `liblerc4` | 4.0.0+ds-5 | lerc | Apache-2.0 |  |
| `libnghttp2-14` | 1.64.0-1.1+deb13u1 | nghttp2 | MIT |  |
| `libnghttp3-9` | 1.8.0-1 | nghttp3 | MIT |  |
| `libngtcp2-16` | 1.11.0-1+deb13u1 | ngtcp2 | MIT |  |
| `libngtcp2-crypto-gnutls8` | 1.11.0-1+deb13u1 | ngtcp2 | MIT |  |
| `libnpth0t64` | 1.8-3 | npth | LGPL-2.1-or-later |  |
| `libnspr4` | 2:4.36-1 | nspr | MPL-2.0 |  |
| `libnss3` | 2:3.110-1+deb13u5 | nss | MPL-2.0 |  |
| `libopenjp2-7` | 2.5.3-2.1~deb13u2 | openjpeg2 | BSD-2-Clause |  |
| `libp11-kit0` | 0.25.5-3 | p11-kit | BSD-3-Clause |  |
| `libpixman-1-0` | 0.44.0-3 | pixman | MIT |  |
| `libpng16-16t64` | 1.6.48-1+deb13u6 | libpng1.6 | libpng-2.0 |  |
| `libpoppler147` | 25.03.0-5+deb13u4 | poppler | GPL-2.0-only or GPL-3.0-only | **yes** |
| `libpsl5t64` | 0.21.2-1.1+b1 | libpsl | MIT |  |
| `librtmp1` | 2.4+20151223.gitfa8646d.1-2+b5 | rtmpdump | LGPL-2.1-or-later |  |
| `libsasl2-2` | 2.1.28+dfsg1-9 | cyrus-sasl2 | BSD-3-Clause-Attribution (CMU) |  |
| `libsasl2-modules-db` | 2.1.28+dfsg1-9 | cyrus-sasl2 | BSD-3-Clause-Attribution (CMU) |  |
| `libsharpyuv0` | 1.5.0-0.1 | libwebp | BSD-3-Clause |  |
| `libssh2-1t64` | 1.11.1-1+deb13u2 | libssh2 | BSD-3-Clause |  |
| `libtasn1-6` | 4.20.0-2+deb13u1 | libtasn1-6 | LGPL-2.1-or-later |  |
| `libtiff6` | 4.7.0-3+deb13u3 | tiff | libtiff (MIT-style) |  |
| `libunistring5` | 1.3-2 | libunistring | LGPL-3.0-or-later or GPL-2.0-or-later |  |
| `libwebp7` | 1.5.0-0.1 | libwebp | BSD-3-Clause |  |
| `libx11-6` | 2:1.8.12-1 | libx11 | MIT/X11 |  |
| `libx11-data` | 2:1.8.12-1 | libx11 | MIT/X11 |  |
| `libxau6` | 1:1.0.11-1 | libxau | MIT/X11 |  |
| `libxcb1` | 1.17.0-2+b1 | libxcb | MIT/X11 |  |
| `libxcb-render0` | 1.17.0-2+b1 | libxcb | MIT/X11 |  |
| `libxcb-shm0` | 1.17.0-2+b1 | libxcb | MIT/X11 |  |
| `libxdmcp6` | 1:1.1.5-1 | libxdmcp | MIT/X11 |  |
| `libxext6` | 2:1.3.4-1+b3 | libxext | MIT/X11 |  |
| `libxrender1` | 1:0.9.12-1 | libxrender | MIT/X11 |  |
| `poppler-utils` | 25.03.0-5+deb13u4 | poppler | GPL-2.0-only or GPL-3.0-only | **yes** |

## Data files of this project

`barcode_decoder/selftest.png`, `barcode_decoder/selftest.heic` and `tests/fixtures/aztec.heic` hold a fictional
Aztec code (the default test boarding pass, ROSSI/MARIO FR 1234 BLQ-STN), made by
`tests/fixtures/make_fixtures.py`; `barcode_decoder/icon.png` and `logo.png` were drawn for this project. MIT, as
the code.

## Regenerating this list

```sh
docker run --rm --entrypoint sh local/barcode-decoder:1.0.0 -c \
  'dpkg-query -W -f "${Package}|${Version}|${source:Package}\n"'      # compare with the base image's list
docker run --rm local/barcode-decoder:1.0.0 python -I -B -c \
  'import pi_heif; print(pi_heif.libheif_info())'
unzip -l <wheel>                                                       # the bundled .so files of each wheel
```
