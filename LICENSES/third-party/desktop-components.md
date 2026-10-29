# Desktop components and source materials

The rc3 candidate (2026-10-29) retains the versioned source materials below.
The actual final inventory must reconcile all six models: three RapidOCR models
and three ONNX Runtime examples. Pypdf 6.21.2, Undici 8.12.2 and Zod 4.6.7 retain
their upstream license texts. This update does not grant new distribution rights.
Psycopg's exact Windows vcpkg/toolchain/build provenance remains a disclosed
reproducibility limit; same-version source archives alone do not prove that build.
The original dated inventory follows; final artifact hashes take precedence.

Engineering inventory for the Windows 0.2.0rc2 candidate, reviewed 2026-10-23.
This is not blanket legal clearance. The actual bundle manifest and dependency
lock, not a developer environment, determine what is distributed. Original
third-party terms prevail over ProjectWatch's own license.

## Libraries with source-delivery obligations

| Included component | Upstream source for this version | Evidence / delivery requirement |
| --- | --- | --- |
| pystray 0.19.5 | https://github.com/moses-palmer/pystray/tree/v0.19.5 | LGPLv3; retain COPYING and COPYING.LGPL; versioned source archive prepared |
| psycopg and psycopg-binary 3.3.6 | https://github.com/psycopg/psycopg/tree/3.3.6 | LGPLv3; full source includes Cython, binary transformation and build scripts; compiled wheel also contains generated C |
| psycopg-pool 3.3.3 | https://pypi.org/project/psycopg-pool/3.3.3/ | LGPLv3; exact sdist prepared and checked against PyPI SHA-256 |
| GEOS 3.14.1, carried by Shapely 2.2.0 | https://download.osgeo.org/geos/geos-3.14.1.tar.bz2 | LGPLv2.1; runtime reports 3.14.1; original license included in Shapely's dist-info; source archive and Shapely 2.2.0 build scripts prepared |
| certifi 2026.10.19 | https://pypi.org/project/certifi/2026.10.19/ | MPL-2.0; exact sdist prepared and checked against PyPI SHA-256 |
| tqdm 4.70.1 | https://pypi.org/project/tqdm/4.70.1/ | MPL-2.0 and MIT; original notices and exact sdist retained |

The final release must offer the corresponding source archive, notices and
build instructions alongside the installer, without requiring a paid account.
Preparing files locally is not equivalent to publishing that source offer.
Do not label this gate passed until the final delivered archive is checked.

These libraries are not frozen into a proprietary executable. After closing
ProjectWatch and backing up data, users can replace interface-compatible Python
libraries/native DLLs in the active version's runtime/Lib/site-packages. Native
extensions must match CPython 3.13 and Windows x64. Original third-party rights,
including debugging modifications, remain intact. Upgrades must not be used to
remove those rights. Uninstall removes listed program files even if modified;
keep modified library sources/binaries outside the program folder before doing so.

### Native dependencies of Psycopg

The actual Windows DLLs report libpq 18.4 and OpenSSL 3.6.4. Their upstream source
archives have been obtained and matched to upstream SHA-256 files:

- https://ftp.postgresql.org/pub/source/v18.4/postgresql-18.4.tar.bz2
- https://github.com/openssl/openssl/releases/tag/openssl-3.6.4

Psycopg's Windows build uses vcpkg and does not pin its port revision in the
versioned workflow. A workflow's Linux/macOS LIBPQ_VERSION is not proof of the
Windows DLL version. Exact upstream Windows build/patch correspondence still
needs checking; possession of similarly named source archives does not prove
bit-for-bit reproducibility. Do not claim that result before it is established.

## Prepackaged OCR models

The RapidAI model repository's **v2.1.0** model card declares Apache License 2.0:
https://modelscope.cn/models/RapidAI/RapidOCR/resolve/v2.1.0/README.md .
The three included RapidOCR models match the upstream versioned download map:

| File | SHA-256 |
| --- | --- |
| ch_PP-OCRv4_det_infer.onnx | d2a7720d45a54257208b1e13e36a8479894cb74155a5efe29462512d42f49da9 |
| ch_PP-OCRv4_rec_infer.onnx | 48fc40f24f6d2a207a2b1091d3437eb3cc3eb6b676dc3ef9c37384005483683b |
| ch_ppocr_mobile_v2.0_cls_infer.onnx | e47acedf663230f8863ff1ab0e64dd2d82b838fceb5957146dab185a89d6215c |

RapidOCR converts PaddleOCR models; preserve RapidAI/PaddleOCR attribution and
the Apache text. This model-card check is independent of the Python code license.
It does not assert rights to every optional downloadable model or font, or to
the underlying training datasets.

ONNX Runtime 1.31.0 also carries three small example models (logreg_iris, mul_1,
sigmoid). Their bytes match the versioned upstream repository under
onnxruntime/python/datasets; its MIT text and third-party notices are retained.
These examples are not company data or additional trained ProjectWatch models.

## Other retained notices

Node, Python, DSH, frontend dependencies, React Bits and fonts keep their existing
notices and boundaries. Neither this checklist nor ProjectWatch's own license
grants additional rights to redistribute React Bits as a standalone library or
template. The final installer/source-export review remains a separate gate.
# Windows dependency command launchers

The Windows builder uses pip's vendored distlib 0.4.2 launchers. Their original
executable prefixes and embedded Python entry-point code are preserved; only the
interpreter shebang is relocated to the bundled Python. Python script entry points
retain their bodies and use a neutral shebang. Installed RECORD hashes are updated.
This does not remove dependency commands, product capabilities or libraries.

The launcher's copyright and BSD-style notice are retained in
`Distlib-Windows-Launcher.txt`, from the fixed upstream source:
https://github.com/pypa/distlib/blob/0.4.2/PC/launcher.c . Its
`<launcher_dir>` support is verified with a native Windows relocation probe.
The vendored distlib package's original `LICENSE.txt` is also copied by the builder
as `Distlib-0.4.2.txt`; neither text is replaced by ProjectWatch's own license.
