#!/usr/bin/env bash
# Compile mac-audio-capture.swift into a native binary.
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  exit 0
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="${SCRIPT_DIR}/mac-audio-capture.swift"
OUT="${SCRIPT_DIR}/mac-audio-capture"

if [[ ! -f "${SRC}" ]]; then
  echo "[mac-audio-capture] Source not found: ${SRC}" >&2
  exit 1
fi

if [[ -f "${OUT}" && "${OUT}" -nt "${SRC}" ]]; then
  echo "[mac-audio-capture] Binary is up to date, skipping compile."
  exit 0
fi

ARCH=$(uname -m)
if [[ "${ARCH}" == "arm64" ]]; then
  TARGET="arm64-apple-macos13.0"
else
  TARGET="x86_64-apple-macos13.0"
fi

CACHE_DIR="/tmp/clang-module-cache"
mkdir -p "${CACHE_DIR}"

echo "[mac-audio-capture] Compiling ${SRC} → ${OUT}"
if ! CLANG_MODULE_CACHE_PATH="${CACHE_DIR}" swiftc \
     "${SRC}" -o "${OUT}" -O -target "${TARGET}" -swift-version 5 \
     -framework ScreenCaptureKit -framework CoreMedia -framework AVFoundation; then
  echo "[mac-audio-capture] swiftc failed." >&2
  echo "[mac-audio-capture] Requires Xcode Command Line Tools and macOS 13+." >&2
  exit 1
fi

echo "[mac-audio-capture] Done."
