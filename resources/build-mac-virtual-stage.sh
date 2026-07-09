#!/usr/bin/env bash
# Compile mac-virtual-stage.swift into a native binary (the off-screen virtual-display stage host).
# Safe to run on non-macOS — exits silently. Mirrors build-mac-ax-helper.sh (same imports:
# AppKit / ApplicationServices / Foundation, all auto-linked — no explicit -framework needed).
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  exit 0
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="${SCRIPT_DIR}/mac-virtual-stage.swift"
OUT="${SCRIPT_DIR}/mac-virtual-stage"

if [[ ! -f "${SRC}" ]]; then
  echo "[mac-virtual-stage] Source not found: ${SRC}" >&2
  exit 1
fi

# Skip rebuild if binary is newer than source
if [[ -f "${OUT}" && "${OUT}" -nt "${SRC}" ]]; then
  echo "[mac-virtual-stage] Binary is up to date, skipping compile."
  exit 0
fi

ARCH=$(uname -m)
if [[ "${ARCH}" == "arm64" ]]; then
  TARGET="arm64-apple-macos12.0"
else
  TARGET="x86_64-apple-macos12.0"
fi

CACHE_DIR="/tmp/clang-module-cache"
mkdir -p "${CACHE_DIR}"

echo "[mac-virtual-stage] Compiling ${SRC} → ${OUT}"
if ! CLANG_MODULE_CACHE_PATH="${CACHE_DIR}" swiftc \
     "${SRC}" -o "${OUT}" -O -target "${TARGET}" -swift-version 5; then
  echo "[mac-virtual-stage] swiftc failed." >&2
  echo "[mac-virtual-stage] Install Xcode Command Line Tools: xcode-select --install" >&2
  exit 1
fi

echo "[mac-virtual-stage] Done."
