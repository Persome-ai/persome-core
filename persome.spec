# PyInstaller spec for persome (macOS, single arch).
#
# 走 spec 而不是 CLI flag：部分依赖（requests/urllib3 的 contrib 子树）
# 是动态导入的，collect_all / collect_submodules
# 在枚举阶段把它们显式收进来，静态扫描看不到。LLM 通路现在全走 Anthropic SDK
# （httpx 传输），不再有 litellm/tiktoken 的特殊处理。
#
# 调用方式（build_python_bundle.sh 会做）：
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

# persome dist-info（运行时通过 importlib.metadata 探测版本）
datas += copy_metadata("persome-core")

# PaddleOCR OCR model weights (PP-OCRv6 tiny tier, bundled ~6MB)
datas += [("ocr_models", "ocr_models")]

# (The optional bge-small-zh slow-pre-gate encoder is no longer bundled; the
#  pregate defaults to regex. To ship it, `pip install onnxruntime tokenizers`,
#  add collect_all("onnxruntime") above, and datas += [("gate_models","gate_models")].)

# PaddleX 在创建 OCR pipeline 时用 importlib.metadata.version(<dep>) 探测每个依赖是否
# 可用（paddlex/utils/deps.py:is_dep_available）；PyInstaller 默认不收第三方包的
# dist-info，导致这些探测全部抛 PackageNotFoundError → "A dependency error occurred
# during pipeline creation"。OCR(-core) extra 声明的依赖必须连 metadata 一起收，
# 同时把纯 Python 的小包（不是 collect_all 传递闭包里的）显式收进来。
for _dep in (
    "paddlex", "paddleocr", "paddlepaddle",
    # ocr / ocr-core extra（paddlex 在 pipeline 创建时逐个 version() 探测）
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

# HTTP 栈：Anthropic SDK 走 httpx；OCR/web-search 等路径走 requests。
# requests/urllib3 的 contrib 子树是动态导入的，必须 collect_all 否则
# 响应解压（gzip/zstandard/brotli）链路缺件，会冒 "Error -3 while
# decompressing data: incorrect header check"。
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
        # AWS providers — 走 deepseek/openai 用不到，省 70MB+
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
# "Persome" — users see "Persome … 想要监控", not "persome". The COLLECT dir
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
