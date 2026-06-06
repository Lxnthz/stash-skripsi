#!/usr/bin/env bash
# =============================================================================
# vm2_service.sh  –  VM2 continuous backup-and-restore service (chain-aware)
# =============================================================================
#
# TWO concurrent workers run inside this process:
#
#   ┌─────────────────────────────────────────────────────────────────────┐
#   │  BACKUP WORKER (background subshell)                                │
#   │  • Scans incoming/<chain-v>/<cycle_id>/ every VM2_POLL_SECONDS      │
#   │  • Encrypts (AES-256-GCM + Base64) → encrypted/<chain-v>/          │
#   │  • Copies raw to permanent/<chain-v>/                               │
#   │  • Deletes incoming cycle dir after success                         │
#   │  • Optional uploads to general / immutable cloud buckets            │
#   └─────────────────────────────────────────────────────────────────────┘
#
#   ┌─────────────────────────────────────────────────────────────────────┐
#   │  RESTORE LISTENER (foreground, event-driven)                        │
#   │  • Watches restore-requests/ with inotifywait (instant wakeup)      │
#   │  • Falls back to polling if inotifywait is unavailable              │
#   │  • On new *.json: pauses backup → decrypts → rsync to VM1 → resumes │
#   │  • Request schema: { version, chain, source, send_to_vm1,           │
#   │                      vm1_dest_root }                                 │
#   └─────────────────────────────────────────────────────────────────────┘
#
# Required:
#   VM2_AES_KEY_B64  – Base64-encoded 32-byte AES-256 key. NEVER logged.
#
# Cloud upload (OPTIONAL – placeholder until buckets configured):
#   VM2_UPLOAD_GENERAL_CMD    – {src} {cycle_id} {chain_v} placeholders
#   VM2_UPLOAD_IMMUTABLE_CMD  – same placeholders
#   VM2_ENABLE_IMMUTABLE_UPLOAD – "1" to enable (default: "0")
#
# Cloud restore (OPTIONAL – placeholder until rclone configured):
#   VM2_RCLONE_GENERAL_REMOTE   – rclone remote:path for general bucket
#   VM2_RCLONE_IMMUTABLE_REMOTE – rclone remote:path for immutable bucket
#
# Usage:
#   VM2_AES_KEY_B64=<key> ./utilities/vm2_service.sh
# =============================================================================
set -euo pipefail
cd /   # never run with a CWD that might be deleted

# ── logging ──────────────────────────────────────────────────────────────────
log() {
  local ts
  ts="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
  echo "[$ts] $*"
}

require_env() {
  local name="$1"
  if [[ -z "${!name:-}" ]]; then
    log "ERROR: required env var $name is not set"
    exit 2
  fi
}

# ── configuration (all overridable via environment) ───────────────────────────
VM2_POLL_SECONDS="${VM2_POLL_SECONDS:-15}"

VM2_INCOMING_DIR="${VM2_INCOMING_DIR:-/home/recovery/local-backup/incoming}"
VM2_PERMANENT_ROOT="${VM2_PERMANENT_ROOT:-/home/recovery/local-backup/permanent}"
VM2_ENCRYPTED_ROOT="${VM2_ENCRYPTED_ROOT:-/home/recovery/local-backup/encrypted}"
VM2_WORK_DIR="${VM2_WORK_DIR:-}"

VM2_DELETE_INCOMING_ON_SUCCESS="${VM2_DELETE_INCOMING_ON_SUCCESS:-1}"

# Immutable uploads disabled by default (objects can't be deleted once written).
VM2_ENABLE_IMMUTABLE_UPLOAD="${VM2_ENABLE_IMMUTABLE_UPLOAD:-0}"

VM2_RESTORE_REQUEST_DIR="${VM2_RESTORE_REQUEST_DIR:-/home/recovery/local-backup/restore-requests}"
VM2_OUTGOING_ROOT="${VM2_OUTGOING_ROOT:-/home/recovery/local-backup/outgoing/primary}"
VM2_VM1_DEST_ROOT_DEFAULT="${VM2_VM1_DEST_ROOT_DEFAULT:-/home/primary/data/backup-incoming}"

STATE_DIR="${VM2_STATE_DIR:-/home/recovery/local-backup/state}"
LOCK_FILE="$STATE_DIR/vm2_service.lock"

# ── cloud placeholders ────────────────────────────────────────────────────────
# General bucket upload (set once bucket is ready):
#   VM2_UPLOAD_GENERAL_CMD="gsutil cp {src} gs://YOUR-BUCKET/{chain_v}/{cycle_id}/"
VM2_UPLOAD_GENERAL_CMD="${VM2_UPLOAD_GENERAL_CMD:-}"

# Immutable bucket upload (also set VM2_ENABLE_IMMUTABLE_UPLOAD=1):
#   VM2_UPLOAD_IMMUTABLE_CMD="gsutil cp {src} gs://YOUR-IMMUTABLE-BUCKET/{chain_v}/{cycle_id}/"
VM2_UPLOAD_IMMUTABLE_CMD="${VM2_UPLOAD_IMMUTABLE_CMD:-}"

# rclone remotes for cloud restore (set once rclone is configured):
#   VM2_RCLONE_GENERAL_REMOTE="mygcs:general-bucket"
#   VM2_RCLONE_IMMUTABLE_REMOTE="mygcs:immutable-bucket"
VM2_RCLONE_GENERAL_REMOTE="${VM2_RCLONE_GENERAL_REMOTE:-}"
VM2_RCLONE_IMMUTABLE_REMOTE="${VM2_RCLONE_IMMUTABLE_REMOTE:-}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_PY="$SCRIPT_DIR/vm2_cycle_processor.py"
RECOVERY_PY="$SCRIPT_DIR/vm2_recovery_sender.py"

# ── global state ──────────────────────────────────────────────────────────────
# PID of the background backup worker subshell.
BACKUP_PID=""

# Named pipe used to signal the restore listener from the backup worker.
SIGNAL_PIPE=""

# ── cleanup ───────────────────────────────────────────────────────────────────
cleanup() {
  log "vm2_service shutting down …"
  if [[ -n "$BACKUP_PID" ]] && kill -0 "$BACKUP_PID" 2>/dev/null; then
    kill "$BACKUP_PID" 2>/dev/null || true
    wait "$BACKUP_PID" 2>/dev/null || true
  fi
  if [[ -n "$SIGNAL_PIPE" && -p "$SIGNAL_PIPE" ]]; then
    rm -f "$SIGNAL_PIPE" 2>/dev/null || true
  fi
  log "vm2_service stopped."
}
trap cleanup EXIT INT TERM

# ── directory setup ───────────────────────────────────────────────────────────
ensure_dirs() {
  mkdir -p \
    "$VM2_INCOMING_DIR" \
    "$VM2_PERMANENT_ROOT" \
    "$VM2_ENCRYPTED_ROOT" \
    "$VM2_RESTORE_REQUEST_DIR" \
    "$VM2_OUTGOING_ROOT" \
    "$STATE_DIR" \
    || true
}

# ── parse restore request JSON ────────────────────────────────────────────────
# Prints key=value lines: version, chain, source, send_to_vm1, vm1_dest_root
parse_request_json() {
  local req_file="$1"
  python3 - <<'PY' "$req_file" "$VM2_VM1_DEST_ROOT_DEFAULT"
import json, sys
from pathlib import Path

req_path = Path(sys.argv[1])
def_vm1  = sys.argv[2]

obj = json.loads(req_path.read_text(encoding='utf-8'))

version = obj.get('version') or obj.get('chain_v')
if not isinstance(version, str) or not version.strip():
    raise SystemExit("missing required field: version")

chain_n = obj.get('chain')
if chain_n is None:
    raise SystemExit("missing required field: chain")
try:
    chain_n = int(chain_n)
except (TypeError, ValueError):
    raise SystemExit("field 'chain' must be an integer")
if chain_n < 1:
    raise SystemExit("field 'chain' must be >= 1")

source = obj.get('source', 'local')
if source not in ('local', 'cloud', 'immutable'):
    raise SystemExit("invalid source (must be local|cloud|immutable)")

send = obj.get('send_to_vm1', True)
if isinstance(send, bool):
    send_to_vm1 = 1 if send else 0
elif isinstance(send, (int, float)):
    send_to_vm1 = 1 if int(send) != 0 else 0
else:
    send_to_vm1 = 1 if str(send).strip().lower() not in ('0','false','no','off','') else 0

vm1_dest = obj.get('vm1_dest_root', def_vm1)
if not isinstance(vm1_dest, str) or not vm1_dest.strip():
    vm1_dest = def_vm1

print(f"version={version.strip()}")
print(f"chain={chain_n}")
print(f"source={source}")
print(f"send_to_vm1={send_to_vm1}")
print(f"vm1_dest_root={vm1_dest.strip()}")
PY
}

# ── backup worker ─────────────────────────────────────────────────────────────
# Called in a background subshell. Loops forever, running the cycle processor
# every VM2_POLL_SECONDS. Writes "tick" to SIGNAL_PIPE after each pass so the
# restore listener knows the backup round has finished.
run_backup_loop() {
  local pipe="$1"

  export VM2_DELETE_INCOMING_ON_SUCCESS
  export VM2_UPLOAD_GENERAL_CMD
  export VM2_UPLOAD_IMMUTABLE_CMD
  export VM2_ENABLE_IMMUTABLE_UPLOAD

  while true; do
    local args
    args=(
      "--once"
      "--incoming-dir"   "$VM2_INCOMING_DIR"
      "--permanent-root" "$VM2_PERMANENT_ROOT"
      "--encrypted-root" "$VM2_ENCRYPTED_ROOT"
    )
    [[ -n "$VM2_WORK_DIR" ]] && args+=("--work-dir" "$VM2_WORK_DIR")

    "$BACKUP_PY" "${args[@]}" || true

    # Signal the restore listener that this backup round completed.
    echo "tick" > "$pipe" 2>/dev/null || true

    sleep "$VM2_POLL_SECONDS"
  done
}

# ── execute one restore request ───────────────────────────────────────────────
execute_restore_request() {
  local req="$1"
  local base done_file err_file inprog

  base="${req%.json}"
  done_file="$base.done"
  err_file="$base.error"
  inprog="$base.inprogress"

  # Atomically claim the request.
  if ! mv "$req" "$inprog" 2>/dev/null; then
    log "restore: could not claim $(basename "$req") (already taken?)"
    return 0
  fi

  log "━━ restore request received: $(basename "$inprog") ━━"

  # Parse.
  local version chain source send_to_vm1 vm1_dest_root
  version=""; chain=""; source="local"; send_to_vm1=1
  vm1_dest_root="$VM2_VM1_DEST_ROOT_DEFAULT"

  local kv_lines
  if ! mapfile -t kv_lines < <(parse_request_json "$inprog" 2>&1); then
    log "ERROR: failed to parse restore request — $(cat "$inprog" 2>/dev/null | head -3)"
    echo "parse_error" > "$err_file"
    mv "$inprog" "$base.json" 2>/dev/null || true
    return 0
  fi

  for line in "${kv_lines[@]}"; do
    case "$line" in
      version=*)     version="${line#version=}";;
      chain=*)       chain="${line#chain=}";;
      source=*)      source="${line#source=}";;
      send_to_vm1=*) send_to_vm1="${line#send_to_vm1=}";;
      vm1_dest_root=*) vm1_dest_root="${line#vm1_dest_root=}";;
    esac
  done

  if [[ -z "$version" || -z "$chain" ]]; then
    log "ERROR: restore request missing version or chain field"
    echo "missing_fields" > "$err_file"
    return 0
  fi

  log "  version  : $version"
  log "  chain    : $chain  (oldest $chain cycle(s))"
  log "  source   : $source"
  log "  send_vm1 : $send_to_vm1"
  [[ "$send_to_vm1" == "1" ]] && log "  vm1_dest : $vm1_dest_root"

  # Build recovery args.
  local args
  args=(
    "--version"        "$version"
    "--chain"          "$chain"
    "--source"         "$source"
    "--permanent-root" "$VM2_PERMANENT_ROOT"
    "--encrypted-root" "$VM2_ENCRYPTED_ROOT"
    "--outgoing-root"  "$VM2_OUTGOING_ROOT"
  )

  if [[ "$send_to_vm1" == "1" ]]; then
    export VM1_RESTORE_DEST_ROOT="$vm1_dest_root"
    args+=("--send-to-vm1")
  fi

  # Cloud source validation.
  if [[ "$source" == "cloud" || "$source" == "immutable" ]]; then
    if ! command -v rclone >/dev/null 2>&1; then
      log "ERROR: rclone not found; required for source=$source"
      echo "rclone_not_found" > "$err_file"
      return 0
    fi
    if [[ "$source" == "cloud" ]]; then
      if [[ -z "${VM2_RCLONE_GENERAL_REMOTE:-}" ]]; then
        log "ERROR: VM2_RCLONE_GENERAL_REMOTE not set (required for source=cloud)"
        echo "missing_rclone_general" > "$err_file"
        return 0
      fi
      export VM2_RCLONE_GENERAL_REMOTE
    else
      if [[ -z "${VM2_RCLONE_IMMUTABLE_REMOTE:-}" ]]; then
        log "ERROR: VM2_RCLONE_IMMUTABLE_REMOTE not set (required for source=immutable)"
        echo "missing_rclone_immutable" > "$err_file"
        return 0
      fi
      export VM2_RCLONE_IMMUTABLE_REMOTE
    fi
  fi

  # Execute.
  if "$RECOVERY_PY" "${args[@]}"; then
    log "━━ restore SUCCEEDED: $version / $chain cycle(s) ━━"
    echo "ok" > "$done_file"
    rm -f "$err_file" "$inprog" 2>/dev/null || true
  else
    log "━━ restore FAILED: $version / $chain cycle(s) ━━"
    echo "failed" > "$err_file"
    log "  → inspect $inprog, then rename to .json to retry"
  fi
}

# ── drain all pending restore requests ───────────────────────────────────────
# Processes every *.json in restore-requests/ that isn't already .done
process_pending_requests() {
  shopt -s nullglob
  local req found=0
  for req in "$VM2_RESTORE_REQUEST_DIR"/*.json; do
    local base="${req%.json}"
    [[ -f "$base.done" ]] && continue   # already finished
    found=1
    execute_restore_request "$req"
  done
  return 0
}

# ── restore listener (main foreground loop) ───────────────────────────────────
# Uses inotifywait if available for instant wakeup, otherwise falls back to
# polling with a short sleep.
run_restore_listener() {
  local pipe="$1"
  local use_inotify=0

  if command -v inotifywait >/dev/null 2>&1; then
    use_inotify=1
    log "restore listener: using inotifywait (instant wakeup on request arrival)"
  else
    log "restore listener: inotifywait not found — using poll fallback (${VM2_POLL_SECONDS}s)"
    log "  → install inotify-tools for instant restore request detection"
  fi

  # Drain anything already sitting in the request dir at startup.
  process_pending_requests

  while true; do
    if [[ "$use_inotify" == "1" ]]; then
      # Block until a file is written/moved into restore-requests/, or timeout.
      # -t timeout ensures we don't block forever if inotifywait dies.
      local event_file=""
      event_file=$(
        inotifywait -q \
          --format "%f" \
          --event close_write,moved_to \
          --timeout "$VM2_POLL_SECONDS" \
          "$VM2_RESTORE_REQUEST_DIR" 2>/dev/null
      ) || true   # timeout exits 1, that's fine

      if [[ -n "$event_file" && "$event_file" == *.json ]]; then
        log "┌─ restore request detected: $event_file"
        # Give writer a moment to fully flush if using close_write.
        sleep 0.2
        process_pending_requests
        log "└─ restore listener: back to watching restore-requests/"
      else
        # Timeout or non-JSON event: just drain in case anything arrived.
        process_pending_requests
      fi

    else
      # Fallback: poll every VM2_POLL_SECONDS.
      # Also consume backup "tick" signals from the pipe so it doesn't fill.
      local pipe_data=""
      read -t "$VM2_POLL_SECONDS" -r pipe_data < "$pipe" 2>/dev/null || true
      process_pending_requests
    fi
  done
}

# ── main ──────────────────────────────────────────────────────────────────────
main() {
  ensure_dirs

  # Dependency checks.
  for dep in python3 sha256sum rsync flock; do
    if ! command -v "$dep" >/dev/null 2>&1; then
      log "ERROR: $dep not found (required)"
      exit 2
    fi
  done

  # AES key required for encryption of every backup cycle.
  require_env VM2_AES_KEY_B64

  # Single-instance lock (prevents two service instances running in parallel).
  exec 9>"$LOCK_FILE"
  if ! flock -n 9; then
    log "ERROR: another vm2_service.sh is already running (lock: $LOCK_FILE)"
    exit 1
  fi

  # ── startup banner ──────────────────────────────────────────────────────────
  log "╔══════════════════════════════════════════════════╗"
  log "║           vm2_service  starting up               ║"
  log "╚══════════════════════════════════════════════════╝"
  log "  incoming     : $VM2_INCOMING_DIR"
  log "  permanent    : $VM2_PERMANENT_ROOT"
  log "  encrypted    : $VM2_ENCRYPTED_ROOT"
  log "  restore-reqs : $VM2_RESTORE_REQUEST_DIR"
  log "  outgoing     : $VM2_OUTGOING_ROOT"
  log "  poll_seconds : $VM2_POLL_SECONDS"

  if [[ -n "$VM2_UPLOAD_GENERAL_CMD" ]]; then
    log "  cloud general  : ENABLED"
  else
    log "  cloud general  : disabled (set VM2_UPLOAD_GENERAL_CMD to enable)"
  fi
  if [[ "$VM2_ENABLE_IMMUTABLE_UPLOAD" == "1" && -n "$VM2_UPLOAD_IMMUTABLE_CMD" ]]; then
    log "  cloud immutable: ENABLED"
  else
    log "  cloud immutable: disabled"
  fi

  # ── named pipe for backup→listener signalling ───────────────────────────────
  SIGNAL_PIPE="$STATE_DIR/vm2_backup.pipe"
  rm -f "$SIGNAL_PIPE" 2>/dev/null || true
  mkfifo "$SIGNAL_PIPE"

  # ── start backup worker in background ──────────────────────────────────────
  log "starting backup worker (background) …"
  run_backup_loop "$SIGNAL_PIPE" &
  BACKUP_PID=$!
  log "backup worker PID: $BACKUP_PID"

  # ── start restore listener in foreground ────────────────────────────────────
  log "starting restore listener (foreground) …"
  log "  drop a JSON file into $VM2_RESTORE_REQUEST_DIR to trigger restore"
  run_restore_listener "$SIGNAL_PIPE"
}

main "$@"
