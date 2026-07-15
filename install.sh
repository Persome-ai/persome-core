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
MODEL_OPEN_SCHEDULED=0
DEFER_UPDATE_COMMIT="${PERSOME_UPDATE_DEFER_COMMIT:-0}"
UPDATE_REPLACEMENT="${PERSOME_UPDATE_REPLACEMENT:-}"
UPDATE_TRANSACTION_ID="${PERSOME_UPDATE_TRANSACTION_ID:-}"
UPDATE_LOCK_FD="${PERSOME_UPDATE_LOCK_FD:-}"

rollback_uncommitted_install() {
  local status=$?
  trap - EXIT INT TERM HUP
  set +e
  if [[ ${status} -ne 0 && ${INSTALL_TRANSACTION_ACTIVE} -eq 1 ]]; then
    if [[ ${UPDATE_MODE} -eq 1 && ${DEFER_UPDATE_COMMIT} -eq 1 ]]; then
      warn "installation failed; discarding the inactive update candidate"
      if [[ -n "${UPDATE_REPLACEMENT}" && -d "${UPDATE_REPLACEMENT}" && ! -L "${UPDATE_REPLACEMENT}" ]]; then
        local failed_candidate="${UPDATE_REPLACEMENT}.failed.$$.$RANDOM"
        if mv "${UPDATE_REPLACEMENT}" "${failed_candidate}"; then
          rm -rf "${failed_candidate}" || true
        else
          warn "could not quarantine the failed update candidate"
        fi
      fi
      exit "${status}"
    fi
    warn "installation failed; restoring the previous virtualenv"
    # Update-mode onboarding can start the replacement daemon before its final
    # capture proof. Stop that process before replacing its on-disk virtualenv;
    # the outer `persome update` command restarts the restored Runtime.
    if [[ ${UPDATE_MODE} -eq 1 && -f "${INSTALL_HOME}/.pid" ]]; then
      if [[ -x "${VENV_DIR}/bin/persome" ]]; then
        PERSOME_ROOT="${INSTALL_HOME}" "${VENV_DIR}/bin/persome" stop --timeout 5 \
          >/dev/null 2>&1 || true
      fi
    fi
    if [[ -n "${OLD_VENV_BACKUP}" && -d "${OLD_VENV_BACKUP}" ]]; then
      # Move the replacement aside first, restore the previous venv with an
      # atomic rename, and only then do the slow recursive cleanup. This keeps
      # the installed shim valid even when Ctrl-C initiated the rollback.
      local failed_venv="${VENV_DIR}.failed.$$.$RANDOM"
      local replacement_moved=0
      if [[ -e "${VENV_DIR}" ]]; then
        if mv "${VENV_DIR}" "${failed_venv}"; then
          replacement_moved=1
        else
          warn "could not move the failed replacement virtualenv aside"
        fi
      fi
      if [[ ! -e "${VENV_DIR}" ]]; then
        if ! mv "${OLD_VENV_BACKUP}" "${VENV_DIR}"; then
          warn "could not restore the previous virtualenv"
          if [[ ${replacement_moved} -eq 1 && -d "${failed_venv}" ]]; then
            mv "${failed_venv}" "${VENV_DIR}" || true
          fi
        fi
      fi
      if [[ -d "${VENV_DIR}" && -e "${failed_venv}" ]]; then
        rm -rf "${failed_venv}" || true
      fi
    else
      rm -rf "${VENV_DIR}" || true
    fi
  fi
  exit "${status}"
}

trap rollback_uncommitted_install EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

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

validate_internal_update_flags() {
  case "${DEFER_UPDATE_COMMIT}" in
    0|1) ;;
    *) die "PERSOME_UPDATE_DEFER_COMMIT must be 0 or 1" ;;
  esac
  if [[ ${DEFER_UPDATE_COMMIT} -eq 1 && ${UPDATE_MODE} -ne 1 ]]; then
    die "deferred update commit requires --update"
  fi
  if [[ ${DEFER_UPDATE_COMMIT} -eq 1 ]]; then
    validate_deferred_update_transaction
  fi
}

validate_deferred_update_transaction() {
  local expected_replacement="${INSTALL_HOME}/venv.replacement.update"
  local state_file="${INSTALL_HOME}/.update-state.json"
  local lock_file="${INSTALL_HOME}/.update.lock"
  local active_python="${INSTALL_HOME}/venv/bin/python"
  [[ "${UPDATE_REPLACEMENT}" == "${expected_replacement}" ]] \
    || die "deferred update candidate must be ${expected_replacement}"
  [[ "${UPDATE_TRANSACTION_ID}" =~ ^[0-9a-f]{32}$ ]] \
    || die "deferred update requires a valid transaction ID"
  [[ "${UPDATE_LOCK_FD}" =~ ^[0-9]+$ ]] \
    || die "deferred update requires the inherited update-lock descriptor"
  [[ -x "${active_python}" ]] \
    || die "deferred update validation requires the active Runtime Python"
  "${active_python}" - \
    "${state_file}" "${lock_file}" "${UPDATE_LOCK_FD}" "${UPDATE_TRANSACTION_ID}" <<'PY' \
    || die "deferred update transaction validation failed"
import fcntl
import json
import os
import stat
import sys

state_path, lock_path, lock_fd_text, expected_id = sys.argv[1:]
lock_fd = int(lock_fd_text)
state_stat = os.lstat(state_path)
lock_stat = os.lstat(lock_path)
fd_stat = os.fstat(lock_fd)
if not stat.S_ISREG(state_stat.st_mode) or stat.S_ISLNK(state_stat.st_mode):
    raise SystemExit("unsafe update state file")
if state_stat.st_uid != os.getuid() or state_stat.st_mode & 0o077:
    raise SystemExit("update state file is not owner-private")
if not stat.S_ISREG(lock_stat.st_mode) or (lock_stat.st_dev, lock_stat.st_ino) != (
    fd_stat.st_dev,
    fd_stat.st_ino,
):
    raise SystemExit("update-lock descriptor does not match the Runtime lock")
# First prove the lock was already held before this validator ran. Then
# re-locking the inherited open-file description proves it is the owner rather
# than an unrelated descriptor blocked by somebody else.
probe_fd = os.open(lock_path, os.O_RDWR)
try:
    try:
        fcntl.flock(probe_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        pass
    else:
        fcntl.flock(probe_fd, fcntl.LOCK_UN)
        raise SystemExit("update lock was not held by the delegating updater")
finally:
    os.close(probe_fd)
fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
with open(state_path, encoding="utf-8") as handle:
    payload = json.load(handle)
if payload != {
    "schema_version": 2,
    "launchagent_was_loaded": payload.get("launchagent_was_loaded"),
    "phase": "preparing",
    "transaction_id": expected_id,
} or not isinstance(payload["launchagent_was_loaded"], bool):
    raise SystemExit("update state does not match the delegated transaction")
PY
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
  # paddlepaddle ships wheels for CPython 3.11-3.13, while Persome requires
  # Python 3.12's sqlite3 db-config API to disable implicit close checkpoints.
  # SQLite 3.42+ is also required so FTS5 secure-delete can remove sensitive
  # shadow-table text. Treat any mismatch as incompatible and fall through to
  # uv-managed Python 3.12.
  if version_ge "${version}" "3.12" && ! version_ge "${version}" "3.14" \
    && python3 - <<'PY' >/dev/null 2>&1
import sqlite3

if sqlite3.sqlite_version_info < (3, 42, 0):
    raise SystemExit(1)
conn = sqlite3.connect(":memory:")
conn.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)
if not conn.getconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE):
    raise SystemExit(1)
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
  [[ ! -L "${VENV_DIR}" ]] || die "virtualenv path must not be a symlink: ${VENV_DIR}"
  if [[ ${UPDATE_MODE} -eq 1 && ${DEFER_UPDATE_COMMIT} -eq 1 ]]; then
    # Build beside the active venv. The outer updater performs one kernel-level
    # directory exchange only after this candidate is complete and marked.
    VENV_DIR="${UPDATE_REPLACEMENT}"
    [[ ! -L "${VENV_DIR}" ]] || die "candidate virtualenv path must not be a symlink: ${VENV_DIR}"
    [[ ! -e "${VENV_DIR}" ]] \
      || die "unfinished update candidate exists at ${VENV_DIR}; refusing to overwrite it"
    OLD_VENV_BACKUP=""
  else
    OLD_VENV_BACKUP="${VENV_DIR}.previous.$$"
    rm -rf "${OLD_VENV_BACKUP}"
  fi
  if [[ ${DEFER_UPDATE_COMMIT} -ne 1 && -d "${VENV_DIR}" ]]; then
    mv "${VENV_DIR}" "${OLD_VENV_BACKUP}"
  fi
  INSTALL_TRANSACTION_ACTIVE=1

  log "creating virtualenv at ${VENV_DIR}"
  if ! "${UV_BIN}" venv "${VENV_DIR}" --python "${python_target}" --relocatable; then
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
conn.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)
if not conn.getconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE):
    raise SystemExit("SQLite refused SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE")
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
  local committed_backup="${OLD_VENV_BACKUP}"
  # The replacement is now authoritative. Mark the transaction committed
  # before deleting the backup so an interrupt during cleanup cannot roll back
  # by first deleting the working virtualenv.
  OLD_VENV_BACKUP=""
  INSTALL_TRANSACTION_ACTIVE=0
  if [[ -n "${committed_backup}" ]]; then
    rm -rf "${committed_backup}" || warn "could not remove old virtualenv backup"
  fi
}

defer_install_commit() {
  [[ ${UPDATE_MODE} -eq 1 && ${DEFER_UPDATE_COMMIT} -eq 1 ]] \
    || die "deferred commit requested outside update mode"
  [[ "${VENV_DIR}" == "${UPDATE_REPLACEMENT}" && -d "${VENV_DIR}" && ! -L "${VENV_DIR}" ]] \
    || die "the inactive update candidate is missing or unsafe"
  "${VENV_DIR}/bin/python" - "${VENV_DIR}/.persome-update-transaction" \
    "${UPDATE_TRANSACTION_ID}" <<'PY' \
    || die "could not persist the update candidate marker"
import os
import sys

path, transaction_id = sys.argv[1:]
descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
try:
    os.write(descriptor, (transaction_id + "\n").encode())
    os.fsync(descriptor)
finally:
    os.close(descriptor)
directory = os.open(os.path.dirname(path), os.O_RDONLY)
try:
    os.fsync(directory)
finally:
    os.close(directory)
PY
  # The outer updater holds the exclusive lock and owns final activation,
  # daemon-owned onboarding proof, exchange, commit, and rollback. Disable this
  # shell's EXIT cleanup only after the complete candidate marker is durable.
  INSTALL_TRANSACTION_ACTIVE=0
  log "replacement prepared; final Runtime proof and commit are owned by persome update"
}

compile_bundled_binaries() {
  log "compiling bundled native helper binaries"
  "${VENV_DIR}/bin/python" - <<'PY' || die "failed to compile bundled native binaries"
import platform

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
if platform.machine().lower() in {"x86_64", "amd64"}:
    from persome.capture.vision_ocr import resolve_helper_path

    vision = resolve_helper_path()
    if vision is None:
        raise SystemExit("mac-vision-ocr not available after Intel install")
    print(f"vision_ocr={vision}")
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
  if [[ ${UPDATE_MODE} -eq 1 && ${DEFER_UPDATE_COMMIT} -eq 1 ]]; then
    PERSOME_ROOT="${INSTALL_HOME}" "${PERSOME_BIN}" --help >/dev/null \
      || die "installation verification failed (new 'persome --help' did not succeed)"
    return 0
  fi
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
  echo "Persome will separately explain and request Accessibility for the bundled"
  echo "capture helper, Accessibility for the optional event watcher, and Screen"
  echo "Recording when the configured pixel policy needs it. It then verifies local"
  echo "OCR, the final Runtime owner, readiness, and a mode-aware capture receipt."
  echo ""
  if ! PERSOME_ROOT="${INSTALL_HOME}" "${INSTALL_BIN_DIR}/persome" onboard --tier tiny; then
    die "onboarding is incomplete; rerun 'persome onboard' to finish permissions and runtime verification"
  fi
  ONBOARDING_COMPLETED=1
}

schedule_model_open() {
  if [[ ${UPDATE_MODE} -eq 1 || ! -t 0 ]]; then
    return 0
  fi

  echo ""
  if PERSOME_ROOT="${INSTALL_HOME}" "${INSTALL_BIN_DIR}/persome" model open --onboarding; then
    MODEL_OPEN_SCHEDULED=1
  else
    warn "could not open setup; open it manually with 'persome model open --onboarding'"
  fi
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

Check Runtime status:
  persome status

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

Inspect Runtime and OCR status any time:
  persome doctor
  persome ocr status

Change or verify the LLM provider:
  persome llm setup
  persome llm status

When those optional features are enabled, run their live probes with:
  persome ocr status --check
  persome llm status --check
EOF

  if [[ ${ONBOARDING_COMPLETED} -eq 1 ]]; then
    cat <<'EOF'

Onboarding proof:
  - The configured capture mode's required macOS permissions were verified.
  - The effective OCR/pixel policy and final Runtime lifecycle owner were proved.
  - A mode-aware fresh-capture, ingest-readiness, or preserved-privacy receipt
    passed through HTTP or the owner-only daemon generation state. Persome does
    not require Full Disk Access or Automation.
EOF
  else
    cat <<'EOF'

Onboarding pending:
  This non-interactive install could not request macOS permissions. From a
  logged-in macOS session, run `persome onboard`; it will not report success
  until the configured mode's permissions, Runtime owner, OCR policy, and
  capture/readiness receipt all pass.
EOF
  fi

  if [[ ${MODEL_OPEN_SCHEDULED} -eq 1 ]]; then
    cat <<'EOF'

MODEL CTA — KEEP PERSOME RUNNING:
  ✓ Unified setup opened in your browser. Choose any existing history, then
    Persome will build and open your local personal model. To reopen setup, run:

      persome model open --onboarding
EOF
  else
    cat <<'EOF'

MODEL CTA — OPEN YOUR PERSONAL MODEL:
  Open unified setup to finish importing history and building your model:

      persome model open --onboarding
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

delegate_existing_install() {
  if [[ ! -e "${VENV_DIR}" ]]; then
    return 0
  fi
  # The current updater invokes this installer with the deferred transaction
  # marker. Any other existing-install invocation (including --update from a
  # previous release) must bootstrap the updater from this source tree first.
  if [[ ${UPDATE_MODE} -eq 1 && ${DEFER_UPDATE_COMMIT} -eq 1 ]]; then
    return 0
  fi
  [[ ! -L "${VENV_DIR}" ]] \
    || die "existing virtualenv must not be a symlink: ${VENV_DIR}"
  [[ -x "${VENV_DIR}/bin/python" ]] \
    || die "existing installation is incomplete (missing ${VENV_DIR}/bin/python); move it aside and rerun the installer"
  log "existing installation detected; bootstrapping the current source-tree updater"
  export PERSOME_ROOT="${INSTALL_HOME}"
  export PERSOME_INSTALL_HOME="${INSTALL_HOME}"
  export PYTHONPATH="${ROOT_DIR}/src"
  export PYTHONNOUSERSITE=1
  if [[ ${UPDATE_MODE} -eq 1 ]]; then
    # Compatibility only: a released updater may have booted launchd out before
    # delegating to this source tree. A direct `bash install.sh --update` has an
    # interactive shell as parent and must not leave a lock keeper tied to that
    # long-lived shell.
    local parent_command
    parent_command="$(ps -ww -p "${PPID}" -o command= 2>/dev/null || true)"
    if [[ "${parent_command}" =~ (^|[[:space:]])([^[:space:]]*/)?persome[[:space:]]+update([[:space:]]|$) ]] \
      || [[ "${parent_command}" =~ [[:space:]]-m[[:space:]]+persome[[:space:]]+update([[:space:]]|$) ]]; then
      export PERSOME_UPDATE_INFER_LAUNCHAGENT_FROM_PLIST=1
    fi
  fi
  exec "${VENV_DIR}/bin/python" -m persome update --source "${ROOT_DIR}"
}

main() {
  parse_args "$@"
  validate_internal_update_flags
  require_repo_root
  delegate_existing_install
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
  if [[ ${UPDATE_MODE} -eq 1 && ${DEFER_UPDATE_COMMIT} -eq 1 ]]; then
    defer_install_commit
    return 0
  fi
  install_shim
  inject_detected_clients
  maybe_configure_llm
  run_onboarding
  schedule_model_open
  if [[ ${UPDATE_MODE} -eq 1 ]]; then
    commit_install
  fi
  print_summary
}

main "$@"
