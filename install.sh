#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

INSTALL_HOME="${PERSOME_INSTALL_HOME:-$HOME/.persome}"
VENV_DIR="${INSTALL_HOME}/venv"
PYTHON_SPEC="${PERSOME_PYTHON:-3.12}"
BIN_DIR_OVERRIDE=""
INJECT_MODE="prompt"  # prompt | all | none
UPDATE_MODE=0

UV_BIN=""
PERSOME_BIN=""
INSTALL_BIN_DIR=""
PYTHON_TARGET=""
INSTALL_TRANSACTION_ACTIVE=0
OLD_VENV_BACKUP=""
ONBOARDING_COMPLETED=0

rollback_uncommitted_install() {
  local status=$?
  trap - EXIT
  if [[ ${status} -ne 0 && ${INSTALL_TRANSACTION_ACTIVE} -eq 1 ]]; then
    warn "installation failed; restoring the previous virtualenv"
    # Update-mode onboarding can start the replacement daemon before its final
    # capture proof. Stop that process before replacing its on-disk virtualenv;
    # the outer `persome update` command restarts the restored Runtime.
    if [[ ${UPDATE_MODE} -eq 1 && -f "${INSTALL_HOME}/.pid" ]]; then
      local update_pid=""
      update_pid="$(tr -d '[:space:]' < "${INSTALL_HOME}/.pid" 2>/dev/null || true)"
      if [[ "${update_pid}" =~ ^[0-9]+$ ]] && kill -0 "${update_pid}" 2>/dev/null; then
        kill -TERM "${update_pid}" 2>/dev/null || true
        local attempts=50
        while (( attempts > 0 )) && kill -0 "${update_pid}" 2>/dev/null; do
          sleep 0.1
          attempts=$((attempts - 1))
        done
      fi
    fi
    rm -rf "${VENV_DIR}"
    if [[ -n "${OLD_VENV_BACKUP}" && -d "${OLD_VENV_BACKUP}" ]]; then
      mv "${OLD_VENV_BACKUP}" "${VENV_DIR}"
    fi
  fi
  exit "${status}"
}

trap rollback_uncommitted_install EXIT

# The fallback bootstrap downloads a specific uv release and verifies the
# archive before executing any of its contents. Update version + both digests
# together after reviewing the upstream release.
UV_BOOTSTRAP_VERSION="0.10.9"
UV_SHA256_AARCH64_DARWIN="a92f61e9ac9b0f29668c15f56152e4a60143fca148ff5bfadb86718472c3f376"
UV_SHA256_X86_64_DARWIN="9cc2de7d195fa157f98b306a8a1cb151ded93f488939b93363cebc8b9d598c28"

log() {
  printf '[persome-install] %s\n' "$*"
}

warn() {
  printf '[persome-install] Warning: %s\n' "$*" >&2
}

die() {
  printf '[persome-install] Error: %s\n' "$*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
Usage: bash install.sh [options]

Installs Persome into a dedicated virtualenv, compiles the macOS AX
helpers, creates a `persome` shim, runs permission/runtime onboarding in an
interactive session, and optionally injects MCP config into detected clients.

Options:
  --python <version>       Python version to target when a managed runtime is needed
                           (default: 3.12)
  --bin-dir <path>         Directory to place the `persome` shim in
  --yes                    Auto-inject all detected MCP client configs
  --no-client-config       Skip MCP client config prompts entirely
  --update                 Preserve existing setup and run the update verification path
  -h, --help               Show this help
EOF
}

version_ge() {
  local lhs="$1"
  local rhs="$2"
  local lhs_major=0 lhs_minor=0 lhs_patch=0
  local rhs_major=0 rhs_minor=0 rhs_patch=0
  local IFS=.

  read -r lhs_major lhs_minor lhs_patch <<< "${lhs}"
  read -r rhs_major rhs_minor rhs_patch <<< "${rhs}"

  lhs_minor="${lhs_minor:-0}"
  lhs_patch="${lhs_patch:-0}"
  rhs_minor="${rhs_minor:-0}"
  rhs_patch="${rhs_patch:-0}"

  if (( lhs_major != rhs_major )); then
    (( lhs_major > rhs_major ))
    return
  fi
  if (( lhs_minor != rhs_minor )); then
    (( lhs_minor > rhs_minor ))
    return
  fi
  (( lhs_patch >= rhs_patch ))
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --python)
        [[ $# -ge 2 ]] || die "--python requires a value"
        PYTHON_SPEC="$2"
        shift 2
        ;;
      --bin-dir)
        [[ $# -ge 2 ]] || die "--bin-dir requires a value"
        BIN_DIR_OVERRIDE="$2"
        shift 2
        ;;
      --yes)
        INJECT_MODE="all"
        shift
        ;;
      --no-client-config)
        INJECT_MODE="none"
        shift
        ;;
      --update)
        UPDATE_MODE=1
        INJECT_MODE="none"
        shift
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        die "unknown option: $1"
        ;;
    esac
  done
}

require_repo_root() {
  [[ -f "${ROOT_DIR}/pyproject.toml" ]] || die "run this script from the repository root"
  [[ -d "${ROOT_DIR}/src/persome" ]] || die "repository layout looks incomplete"
}

check_platform() {
  [[ "$(uname -s)" == "Darwin" ]] || die "Persome currently supports macOS only"
  local product_version
  product_version="$(sw_vers -productVersion 2>/dev/null || true)"
  [[ -n "${product_version}" ]] || die "could not determine macOS version via sw_vers"
  local major
  major="${product_version%%.*}"
  version_ge "${major}" "13" || die "macOS 13+ required (found ${product_version})"
}

ensure_xcode_clt() {
  if xcode-select -p >/dev/null 2>&1 && command -v swiftc >/dev/null 2>&1; then
    return
  fi

  warn "Xcode Command Line Tools are required to compile the AX binaries."
  if command -v xcode-select >/dev/null 2>&1; then
    xcode-select --install >/dev/null 2>&1 || true
  fi
  die "install Xcode Command Line Tools (xcode-select --install), then rerun this script"
}

ensure_uv() {
  if command -v uv >/dev/null 2>&1; then
    UV_BIN="$(command -v uv)"
    return
  fi

  command -v curl >/dev/null 2>&1 || die "uv not found and curl is unavailable"
  command -v shasum >/dev/null 2>&1 || die "uv bootstrap requires shasum"

  local machine target expected archive tmp_dir actual extracted install_dir
  machine="$(uname -m)"
  case "${machine}" in
    arm64)
      target="aarch64-apple-darwin"
      expected="${UV_SHA256_AARCH64_DARWIN}"
      ;;
    x86_64)
      target="x86_64-apple-darwin"
      expected="${UV_SHA256_X86_64_DARWIN}"
      ;;
    *)
      die "unsupported macOS architecture for uv bootstrap: ${machine}"
      ;;
  esac

  archive="uv-${target}.tar.gz"
  tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/persome-uv.XXXXXX")"
  log "uv not found; downloading verified uv ${UV_BOOTSTRAP_VERSION}"
  if ! curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location \
    "https://github.com/astral-sh/uv/releases/download/${UV_BOOTSTRAP_VERSION}/${archive}" \
    --output "${tmp_dir}/${archive}"; then
    rm -rf "${tmp_dir}"
    die "failed to download uv ${UV_BOOTSTRAP_VERSION}"
  fi
  actual="$(shasum -a 256 "${tmp_dir}/${archive}" | awk '{print $1}')"
  if [[ "${actual}" != "${expected}" ]]; then
    rm -rf "${tmp_dir}"
    die "uv archive checksum mismatch"
  fi
  tar -xzf "${tmp_dir}/${archive}" -C "${tmp_dir}" \
    || { rm -rf "${tmp_dir}"; die "failed to extract verified uv archive"; }
  extracted="${tmp_dir}/uv-${target}/uv"
  [[ -x "${extracted}" ]] \
    || { rm -rf "${tmp_dir}"; die "verified uv archive had an unexpected layout"; }

  install_dir="${HOME}/.local/bin"
  mkdir -p "${install_dir}"
  install -m 0755 "${extracted}" "${install_dir}/uv"
  if [[ -x "${tmp_dir}/uv-${target}/uvx" ]]; then
    install -m 0755 "${tmp_dir}/uv-${target}/uvx" "${install_dir}/uvx"
  fi
  rm -rf "${tmp_dir}"
  UV_BIN="${install_dir}/uv"
  export PATH="${install_dir}:${PATH}"
}

find_compatible_system_python() {
  if ! command -v python3 >/dev/null 2>&1; then
    return 1
  fi

  local version
  version="$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:3])))' 2>/dev/null || true)"
  [[ -n "${version}" ]] || return 1
  # paddlepaddle ships wheels for CPython 3.11-3.13 only. Persome also needs
  # SQLite 3.42+ so FTS5 secure-delete can remove sensitive shadow-table text.
  # Treat either mismatch as incompatible and fall through to uv-managed Python.
  if version_ge "${version}" "3.11" && ! version_ge "${version}" "3.14" \
    && python3 - <<'PY' >/dev/null 2>&1
import sqlite3

if sqlite3.sqlite_version_info < (3, 42, 0):
    raise SystemExit(1)
conn = sqlite3.connect(":memory:")
conn.execute("CREATE VIRTUAL TABLE probe USING fts5(body)")
conn.execute("INSERT INTO probe(probe, rank) VALUES('secure-delete', 1)")
PY
  then
    command -v python3
    return 0
  fi
  return 1
}

prepare_python_target() {
  local system_python=""
  if system_python="$(find_compatible_system_python)"; then
    log "using system Python at ${system_python}"
    PYTHON_TARGET="${system_python}"
    return 0
  fi

  log "system Python lacks a supported Python/SQLite runtime; installing managed Python ${PYTHON_SPEC} via uv"
  "${UV_BIN}" python install "${PYTHON_SPEC}" || die "failed to install Python ${PYTHON_SPEC} via uv"
  PYTHON_TARGET="${PYTHON_SPEC}"
}

install_package() {
  local python_target="$1"
  local build_dir
  local -a wheels
  "${UV_BIN}" lock --project "${ROOT_DIR}" --check \
    || die "uv.lock is stale; refusing an unlocked installation"

  build_dir="$(mktemp -d "${TMPDIR:-/tmp}/persome-wheel.XXXXXX")"
  log "building Persome with hash-verified build dependencies"
  # uv 0.10.9 mis-parses an absolute --project value containing spaces. A
  # quoted subshell keeps arbitrary checkout paths intact without changing the
  # caller's working directory.
  if ! (
    cd "${ROOT_DIR}"
    "${UV_BIN}" build --project . --wheel \
      --build-constraints build-constraints.txt --require-hashes \
      --out-dir "${build_dir}"
  ); then
    rm -rf "${build_dir}"
    die "failed to build Persome with the pinned build environment"
  fi
  wheels=("${build_dir}"/persome_core-*.whl)
  if [[ ${#wheels[@]} -ne 1 || ! -f "${wheels[0]}" ]]; then
    rm -rf "${build_dir}"
    die "expected exactly one built Persome wheel"
  fi

  mkdir -p "${INSTALL_HOME}"
  chmod 0700 "${INSTALL_HOME}"
  OLD_VENV_BACKUP="${VENV_DIR}.previous.$$"
  rm -rf "${OLD_VENV_BACKUP}"
  if [[ -d "${VENV_DIR}" ]]; then
    mv "${VENV_DIR}" "${OLD_VENV_BACKUP}"
  fi
  INSTALL_TRANSACTION_ACTIVE=1

  log "creating virtualenv at ${VENV_DIR}"
  if ! "${UV_BIN}" venv "${VENV_DIR}" --python "${python_target}"; then
    rm -rf "${build_dir}"
    die "failed to create virtualenv"
  fi

  log "installing locked binary dependencies into the virtualenv"
  if ! UV_PROJECT_ENVIRONMENT="${VENV_DIR}" \
    "${UV_BIN}" sync --project "${ROOT_DIR}" --locked --no-dev \
      --no-install-project --no-build --python "${python_target}"; then
    rm -rf "${build_dir}"
    die "failed to install Persome dependencies into ${VENV_DIR}"
  fi

  if ! "${VENV_DIR}/bin/python" - "${python_target}" <<'PY'
import re
import sqlite3
import sys

requested = sys.argv[1]
match = re.fullmatch(r"(\d+)\.(\d+)(?:\.\d+)?", requested)
if match and sys.version_info[:2] != (int(match.group(1)), int(match.group(2))):
    raise SystemExit(
        f"requested Python {requested}, got {sys.version_info.major}.{sys.version_info.minor}"
    )
if sqlite3.sqlite_version_info < (3, 42, 0):
    raise SystemExit(f"SQLite 3.42+ required, got {sqlite3.sqlite_version}")
conn = sqlite3.connect(":memory:")
conn.execute("CREATE VIRTUAL TABLE probe USING fts5(body)")
conn.execute("INSERT INTO probe(probe, rank) VALUES('secure-delete', 1)")
PY
  then
    rm -rf "${build_dir}"
    die "installed Python/SQLite runtime failed compatibility verification"
  fi

  log "installing the verified local Persome wheel"
  if ! "${UV_BIN}" pip install --python "${VENV_DIR}/bin/python" --no-deps "${wheels[0]}"; then
    rm -rf "${build_dir}"
    die "failed to install the verified Persome wheel into ${VENV_DIR}"
  fi
  rm -rf "${build_dir}"

  PERSOME_BIN="${VENV_DIR}/bin/persome"
  [[ -x "${PERSOME_BIN}" ]] || die "expected CLI not found at ${PERSOME_BIN}"
}

commit_install() {
  if [[ -n "${OLD_VENV_BACKUP}" ]]; then
    rm -rf "${OLD_VENV_BACKUP}"
  fi
  OLD_VENV_BACKUP=""
  INSTALL_TRANSACTION_ACTIVE=0
}

compile_bundled_binaries() {
  log "compiling bundled AX helper binaries"
  "${VENV_DIR}/bin/python" - <<'PY' || die "failed to compile bundled AX binaries"
from persome.capture.ax_capture import _resolve_helper_path
from persome.capture.watcher import _resolve_watcher_path

helper = _resolve_helper_path()
watcher = _resolve_watcher_path()
if helper is None:
    raise SystemExit("mac-ax-helper not available after install")
if watcher is None:
    raise SystemExit("mac-ax-watcher not available after install")
print(f"helper={helper}")
print(f"watcher={watcher}")
PY
}

choose_install_bin_dir() {
  if [[ -n "${BIN_DIR_OVERRIDE}" ]]; then
    mkdir -p "${BIN_DIR_OVERRIDE}"
    [[ -w "${BIN_DIR_OVERRIDE}" ]] || die "--bin-dir is not writable: ${BIN_DIR_OVERRIDE}"
    printf '%s' "${BIN_DIR_OVERRIDE}"
    return 0
  fi

  local home_local="${HOME}/.local/bin"
  mkdir -p "${home_local}"
  [[ -w "${home_local}" ]] || die "could not create a writable bin directory at ${home_local}"
  printf '%s' "${home_local}"
}

install_shim() {
  INSTALL_BIN_DIR="$(choose_install_bin_dir)"
  local shim_path="${INSTALL_BIN_DIR}/persome"
  local quoted_bin quoted_root
  printf -v quoted_bin '%q' "${PERSOME_BIN}"
  printf -v quoted_root '%q' "${INSTALL_HOME}"
  # Runtime expansion in the generated shim is intentional.
  # shellcheck disable=SC2016
  printf '%s\n' \
    '#!/usr/bin/env bash' \
    'set -euo pipefail' \
    'if [[ -z "${PERSOME_ROOT:-}" ]]; then' \
    "  export PERSOME_ROOT=${quoted_root}" \
    'fi' \
    "exec ${quoted_bin} \"\$@\"" > "${shim_path}"
  chmod 0755 "${shim_path}"
  export PATH="${INSTALL_BIN_DIR}:${PATH}"
  log "installed persome shim at ${shim_path}"
}

verify_install() {
  PERSOME_ROOT="${INSTALL_HOME}" "${PERSOME_BIN}" status >/dev/null \
    || die "installation verification failed ('persome status' did not succeed)"
}

prompt_yes_no() {
  local prompt="$1"
  local reply
  if [[ ! -t 0 ]]; then
    return 1
  fi
  read -r -p "${prompt} [Y/n] " reply
  if [[ -z "${reply}" ]]; then
    return 0
  fi
  [[ "${reply}" =~ ^([Yy]|[Yy][Ee][Ss])$ ]]
}

maybe_inject_client() {
  local client="$1"
  local label="$2"

  case "${INJECT_MODE}" in
    none)
      return 0
      ;;
    all)
      log "injecting MCP config into ${label}"
      ;;
    prompt)
      if ! prompt_yes_no "Detected ${label}. Inject Persome MCP config now?"; then
        return 0
      fi
      ;;
    *)
      die "unexpected INJECT_MODE=${INJECT_MODE}"
      ;;
  esac

  if ! "${INSTALL_BIN_DIR}/persome" install "${client}"; then
    warn "failed to inject MCP config for ${label}; you can retry later with 'persome install ${client}'"
  fi
}

inject_detected_clients() {
  local codex_cfg="$HOME/.codex/config.toml"
  local claude_code_cfg="$HOME/.claude.json"
  local claude_desktop_cfg="$HOME/Library/Application Support/Claude/claude_desktop_config.json"
  local opencode_cfg="$HOME/.config/opencode/opencode.json"
  local opencode_jsonc="$HOME/.config/opencode/opencode.jsonc"

  if [[ -f "${codex_cfg}" ]]; then
    if command -v codex >/dev/null 2>&1; then
      maybe_inject_client "codex" "Codex CLI"
    else
      warn "found ${codex_cfg}, but \`codex\` is not on PATH; skipping MCP injection"
    fi
  fi

  if [[ -f "${claude_code_cfg}" ]]; then
    if command -v claude >/dev/null 2>&1; then
      maybe_inject_client "claude-code" "Claude Code"
    else
      warn "found ${claude_code_cfg}, but \`claude\` is not on PATH; skipping MCP injection"
    fi
  fi

  if [[ -f "${claude_desktop_cfg}" ]]; then
    maybe_inject_client "claude-desktop" "Claude Desktop"
  fi

  if [[ -f "${opencode_cfg}" || -f "${opencode_jsonc}" ]]; then
    maybe_inject_client "opencode" "opencode"
  fi
}

maybe_configure_llm() {
  local config_path="${INSTALL_HOME}/config.toml"

  if [[ ! -f "${config_path}" ]]; then
    log "creating default config at ${config_path}"
    if ! "${VENV_DIR}/bin/python" - "${ROOT_DIR}/src" <<'PY'
import sys

sys.path.insert(0, sys.argv[1])
from persome.config import write_default_if_missing

write_default_if_missing()
PY
    then
      warn "failed to create default config; you can create it manually later"
    fi
  fi

  if [[ ${UPDATE_MODE} -eq 1 ]]; then
    log "update mode: preserving the existing LLM profile and credentials"
    return 0
  fi

  if [[ ! -t 0 ]]; then
    log "non-interactive install: run 'persome llm setup' to configure a provider"
    return 0
  fi

  echo ""
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "  LLM Provider Setup (bring your own key or local model)"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo ""
  echo "Choose your LLM provider and enter its API key. Persome selects the"
  echo "endpoint and default model, then verifies completion and tool calling"
  echo "before saving anything. Existing keys are detected automatically."
  echo ""
  if ! prompt_yes_no "Configure and test the Runtime LLM now?"; then
    echo "Skipped. Run 'persome llm setup' at any time."
    return 0
  fi
  if ! "${INSTALL_BIN_DIR}/persome" llm setup; then
    warn "LLM setup was not completed; rerun it later with 'persome llm setup'"
  fi

  echo ""
  echo "Optional: for semantic (paraphrase-robust) memory search, also set"
  echo "OPENAI_* embedding credentials in ${INSTALL_HOME}/env. Without them the"
  echo "daemon runs keyword (BM25) search only — no degraded behaviour."
  echo ""
  echo "A machine-local screenshot encryption key is generated automatically;"
  echo "you never need to enter or manage it manually."
}

run_onboarding() {
  if [[ ! -t 0 ]]; then
    if [[ ${UPDATE_MODE} -eq 1 ]]; then
      log "non-interactive update: verifying existing permissions and Runtime health"
      if ! PERSOME_ROOT="${INSTALL_HOME}" "${INSTALL_BIN_DIR}/persome" onboard --tier tiny --no-gui; then
        die "update installed, but non-interactive Runtime verification failed; rerun 'persome onboard'"
      fi
      ONBOARDING_COMPLETED=1
      return 0
    fi
    log "non-interactive install: run 'persome onboard' from a logged-in macOS session"
    return 0
  fi

  echo ""
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "  Permission and Runtime Onboarding"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo ""
  echo "Persome will explain and request Accessibility and Screen Recording in"
  echo "separate macOS dialogs. It then verifies local OCR, starts the daemon,"
  echo "checks the local health endpoint, and writes one fresh capture."
  echo ""
  if ! PERSOME_ROOT="${INSTALL_HOME}" "${INSTALL_BIN_DIR}/persome" onboard --tier tiny; then
    die "onboarding is incomplete; rerun 'persome onboard' to finish permissions and runtime verification"
  fi
  ONBOARDING_COMPLETED=1
}

ensure_screenshot_key() {
  local env_path="${INSTALL_HOME}/env"
  local status
  status="$("${VENV_DIR}/bin/python" - "${env_path}" <<'PY'
import sys
from pathlib import Path

from persome.env_file import ensure_screenshot_key

print(ensure_screenshot_key(Path(sys.argv[1])))
PY
)" || die "failed to provision PERSOME_SCREENSHOT_KEY"

  case "${status}" in
    existing)
      log "screenshot encryption key already configured"
      ;;
    generated)
      log "generated machine-local screenshot encryption key in ${env_path}"
      ;;
    *)
      die "unexpected screenshot-key provisioning result: ${status}"
      ;;
  esac
}

ensure_local_api_token() {
  local env_path="${INSTALL_HOME}/env"
  local status
  status="$("${VENV_DIR}/bin/python" - "${env_path}" <<'PY'
import sys
from pathlib import Path

from persome.env_file import ensure_local_api_token

print(ensure_local_api_token(Path(sys.argv[1])))
PY
)" || die "failed to provision PERSOME_LOCAL_API_TOKEN"

  case "${status}" in
    existing)
      log "local API bearer token already configured"
      ;;
    generated)
      log "generated machine-local API bearer token in ${env_path}"
      ;;
    *)
      die "unexpected local API token provisioning result: ${status}"
      ;;
  esac
}

print_summary() {
  cat <<EOF

Persome installed successfully.

Install root : ${INSTALL_HOME}
Virtualenv   : ${VENV_DIR}
CLI shim     : ${INSTALL_BIN_DIR}/persome

Next steps:
  1. Check status:
     persome status
  2. Open the live personal-model viewer:
     persome model open

Event memory can appear during a session's five-minute flushes. Points and Lines
are modeled from each successful flush while the daemon keeps running. Face,
Volume, and Root need repeated evidence and refresh in the background. The
viewer refreshes itself; stopping Persome is never a modeling step.

Connect an agent (MCP):
  Prefer the owner-local stdio transport (no bearer token copied into client config):
    persome install claude-code
    persome install codex
    persome install claude-desktop
    persome install opencode

Run a health check any time:
  persome doctor
  persome ocr status --check

Change or verify the LLM provider:
  persome llm setup
  persome llm status --check
EOF

  if [[ ${ONBOARDING_COMPLETED} -eq 1 ]]; then
    cat <<'EOF'

Onboarding proof:
  - Accessibility was granted for focused AX text and structure.
  - Screen Recording and the isolated local OCR worker were verified.
  - Persome was started, its local health endpoint passed, and a fresh capture
    record was written. Persome does not require Full Disk Access or Automation.
EOF
  else
    cat <<'EOF'

Onboarding pending:
  This non-interactive install could not request macOS permissions. From a
  logged-in macOS session, run `persome onboard`; it will not report success
  until Accessibility, local OCR, daemon health, and a fresh capture all pass.
EOF
  fi

  case ":${PATH}:" in
    *":${INSTALL_BIN_DIR}:"*)
      ;;
    *)
      warn "${INSTALL_BIN_DIR} is not on your PATH in this shell. Add it before using 'persome' from a new terminal."
      ;;
  esac
}

main() {
  parse_args "$@"
  require_repo_root
  check_platform
  ensure_xcode_clt
  ensure_uv

  prepare_python_target
  [[ -n "${PYTHON_TARGET}" ]] || die "failed to determine a Python target"
  install_package "${PYTHON_TARGET}"
  ensure_screenshot_key
  ensure_local_api_token
  compile_bundled_binaries
  verify_install
  # Fresh installs keep the established commit point. Updates remain
  # transactional through onboarding so a failed health/capture proof can
  # restore the previous venv.
  if [[ ${UPDATE_MODE} -eq 0 ]]; then
    commit_install
  fi
  install_shim
  inject_detected_clients
  maybe_configure_llm
  run_onboarding
  if [[ ${UPDATE_MODE} -eq 1 ]]; then
    commit_install
  fi
  print_summary
}

main "$@"
