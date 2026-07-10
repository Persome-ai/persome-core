#!/usr/bin/env bash
set -euo pipefail

INSTALL_HOME="${PERSOME_INSTALL_HOME:-$HOME/.persome}"
BIN_DIR="${PERSOME_BIN_DIR:-$HOME/.local/bin}"
DELETE_DATA=false
ASSUME_YES=false

log() {
  printf '[persome-uninstall] %s\n' "$*"
}

warn() {
  printf '[persome-uninstall] Warning: %s\n' "$*" >&2
}

usage() {
  printf '%s\n' \
    'Usage: bash uninstall.sh [options]' \
    '' \
    'Removes the Persome daemon, LaunchAgent, CLI shim, and dedicated virtualenv.' \
    'Personal data and configuration are kept unless --delete-data is supplied.' \
    '' \
    'Options:' \
    '  --delete-data         Also delete config, env, captures, memory, exports, and logs' \
    '  --yes                 Skip the DELETE confirmation required by --delete-data' \
    '  --install-home PATH   Override the install/data root' \
    '  --bin-dir PATH        Override the directory containing the persome shim' \
    '  -h, --help            Show this help'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --delete-data)
      DELETE_DATA=true
      shift
      ;;
    --yes)
      ASSUME_YES=true
      shift
      ;;
    --install-home)
      [[ $# -ge 2 ]] || { warn '--install-home requires a path'; exit 2; }
      INSTALL_HOME="$2"
      shift 2
      ;;
    --bin-dir)
      [[ $# -ge 2 ]] || { warn '--bin-dir requires a path'; exit 2; }
      BIN_DIR="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      warn "unknown option: $1"
      usage
      exit 2
      ;;
  esac
done

INSTALL_HOME="$(cd "$(dirname "$INSTALL_HOME")" 2>/dev/null && pwd)/$(basename "$INSTALL_HOME")"
VENV_DIR="$INSTALL_HOME/venv"
PERSOME_BIN="$VENV_DIR/bin/persome"
SHIM="$BIN_DIR/persome"

if [[ "$INSTALL_HOME" == "/" || "$INSTALL_HOME" == "$HOME" ]]; then
  warn "refusing unsafe install root: $INSTALL_HOME"
  exit 2
fi

if [[ -x "$PERSOME_BIN" ]]; then
  "$PERSOME_BIN" stop >/dev/null 2>&1 || true
  "$PERSOME_BIN" launchagent uninstall >/dev/null 2>&1 || true
else
  launchctl bootout "gui/$(id -u)/com.persome.runtime" >/dev/null 2>&1 || true
  rm -f "$HOME/Library/LaunchAgents/com.persome.runtime.plist"
fi

if [[ -f "$SHIM" ]]; then
  if grep -Fq "$PERSOME_BIN" "$SHIM"; then
    rm -f "$SHIM"
    log "removed CLI shim: $SHIM"
  else
    warn "left $SHIM untouched because it does not point to $PERSOME_BIN"
  fi
fi

if [[ -d "$VENV_DIR" ]]; then
  rm -rf "$VENV_DIR"
  log "removed virtualenv: $VENV_DIR"
fi

if [[ "$DELETE_DATA" == true ]]; then
  if [[ "$ASSUME_YES" != true ]]; then
    [[ -t 0 ]] || { warn '--delete-data in a non-interactive shell requires --yes'; exit 2; }
    printf 'Type DELETE to remove all Persome data under %s: ' "$INSTALL_HOME"
    read -r reply
    [[ "$reply" == "DELETE" ]] || { log 'data deletion cancelled'; exit 1; }
  fi
  rm -rf "$INSTALL_HOME"
  log "deleted Persome data root: $INSTALL_HOME"
else
  log "kept personal data and configuration under $INSTALL_HOME"
  log "run 'bash uninstall.sh --delete-data --yes' to delete it explicitly"
fi

log 'uninstall complete'
