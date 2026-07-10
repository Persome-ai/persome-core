# PyInstaller spec for persome (macOS, single arch).
#
# Use a spec because several dependencies dynamically import modules that a
# static scan cannot see. collect_all and collect_submodules include them
# explicitly. LLM traffic uses the Anthropic SDK over httpx.
#
# Invocation used by build_python_bundle.sh:
#   pyinstaller --noconfirm \
#     --workpath build/python-bundle/.work \
#     --distpath build/python-bundle/.dist \
#     persome.spec

from PyInstaller.utils.hooks import (
    collect_all,
    collect_submodules,
    copy_metadata,
)

block_cipher = None


def _merge(*triples):
    datas, binaries, hiddens = [], [], []
    for d, b, h in triples:
        datas += d
        binaries += b
        hiddens += h
    return datas, binaries, hiddens


def _safe_collect(pkg):
    """collect_all but silently skips packages that are not installed (e.g. paddle on x86_64)."""
    try:
        return collect_all(pkg)
    except Exception:
        return [], [], []


datas, binaries, hiddenimports = _merge(
    collect_all("persome"),
    collect_all("fastapi"),
    collect_all("uvicorn"),
    collect_all("mcp"),
    collect_all("typer"),
    collect_all("rich"),
    # PaddleOCR for on-device OCR (PP-OCRv6 tiny tier) — arm64 macOS only;
    # paddle has no x86_64 macOS wheel, so these are no-ops on x86_64 builds.
    _safe_collect("paddleocr"),
    _safe_collect("paddlepaddle"),
    _safe_collect("paddlex"),
    _safe_collect("cv2"),          # opencv-contrib-python imports as `cv2`
    collect_all("shapely"),
)

# persome dist-info, used by importlib.metadata at runtime
datas += copy_metadata("persome-core")

# PaddleOCR OCR model weights (PP-OCRv6 tiny tier, bundled ~6MB)
datas += [("ocr_models", "ocr_models")]

# (The optional bge-small-zh slow-pre-gate encoder is no longer bundled; the
#  pregate defaults to regex. To ship it, `pip install onnxruntime tokenizers`,
#  add collect_all("onnxruntime") above, and datas += [("gate_models","gate_models")].)

# PaddleX probes dependencies through importlib.metadata.version while creating
# an OCR pipeline. PyInstaller omits third-party dist-info by default, so OCR
# dependencies and their metadata must be collected explicitly.
for _dep in (
    "paddlex", "paddleocr", "paddlepaddle",
    # OCR dependencies probed through version() during pipeline creation.
    "opencv-contrib-python", "pyclipper", "pypdfium2", "python-bidi",
    "shapely", "imagesize", "lxml", "scikit-learn", "scipy",
    "safetensors", "einops", "ftfy", "regex",
    "beautifulsoup4", "Jinja2", "latex2mathml", "openpyxl",
    "premailer", "sentencepiece", "tiktoken",
):
    try:
        datas += copy_metadata(_dep)
    except Exception:
        pass  # not installed (some are optional under different extras) → skip

# Pure-Python OCR deps that aren't pulled in as a collect_all transitive closure.
for _mod in ("imagesize", "pyclipper", "bidi", "einops", "ftfy",
             "latex2mathml", "premailer", "safetensors"):
    try:
        hiddenimports += collect_submodules(_mod)
    except Exception:
        pass

# HTTP stack: the Anthropic SDK uses httpx; OCR and optional web paths use
# requests. requests/urllib3 contrib modules are dynamic and must be collected
# so gzip, zstandard, and brotli responses can be decompressed.
hiddenimports += collect_submodules("httpx")
hiddenimports += collect_submodules("pydantic")
datas_r, binaries_r, hidden_r = collect_all("requests")
datas += datas_r
binaries += binaries_r
hiddenimports += hidden_r
datas_u, binaries_u, hidden_u = collect_all("urllib3")
datas += datas_u
binaries += binaries_u
hiddenimports += hidden_u
datas += copy_metadata("requests")
datas += copy_metadata("urllib3")
hiddenimports += collect_submodules("charset_normalizer")

a = Analysis(
    ["pyi_entrypoint.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=["pyi_rthook_ssl.py"],
    excludes=[
        # AWS providers are unused and add more than 70 MB.
        "botocore",
        "boto3",
        "sagemaker",
    ],
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# The bootloader filename is what macOS shows in TCC prompts / the Privacy
# panes: a bare executable has no Info.plist, so the OS falls back to the
# filename. The daemon is the process that requests Accessibility, so name it
# Use "Persome" so macOS permission prompts show the product name. The COLLECT dir
# below stays "persome", so the on-disk layout (and every path that
# references it) is unchanged; only the inner binary is renamed.
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Persome Backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    target_arch=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="persome",
)
