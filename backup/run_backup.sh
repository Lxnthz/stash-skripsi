#!/usr/bin/env bash
# =============================================================================
# run_backup.sh — RPO-controlled backup loop for the Primary VM
#
# Runs backup_orchestrator.py every RPO_MINUTES, which:
#   1. Takes a snapshot of PG WAL + Mongo oplog + PFC unstructured data
#   2. Stores the hashed cycle under  backup-cycles/chain-vN/<cycle_id>/
#   3. Transfers the cycle to the recovery VM via rsync (TRANSFER_ENABLE=1)
#
# Usage:
#   sudo bash utilities/backup/run_backup.sh
#
# Or as non-root with a sudoers NOPASSWD entry:
#   bash utilities/backup/run_backup.sh
#
# To stop:
#   Ctrl+C  (the trap will print a clean exit message)
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# ★ CONFIGURE HERE — tweak these to change the RPO
# ---------------------------------------------------------------------------
RPO_MINUTES=5              # How often to run a backup cycle (Recovery Point Objective)
MAX_CONSECUTIVE_FAILS=3     # Abort the loop after this many back-to-back failures

# ---------------------------------------------------------------------------
# Paths (relative to this script's directory, so moveable)
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_ENV="${SCRIPT_DIR}/backup.env"
ORCHESTRATOR="${SCRIPT_DIR}/backup_orchestrator.py"

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------
if [[ ! -f "${BACKUP_ENV}" ]]; then
    echo "[FAIL] backup.env not found at: ${BACKUP_ENV}" >&2
    exit 1
fi
if [[ ! -f "${ORCHESTRATOR}" ]]; then
    echo "[FAIL] backup_orchestrator.py not found at: ${ORCHESTRATOR}" >&2
    exit 1
fi

# Source environment (sets TRANSFER_ENABLE, RECOVERY_RSYNC_TARGETS, SSH key, etc.)
# shellcheck source=backup.env
source "${BACKUP_ENV}"

# ---------------------------------------------------------------------------
# Must run as root (backup tools need access to docker data dirs).
# If not root, re-exec via sudo -E (preserves env from backup.env).
# ---------------------------------------------------------------------------
if [[ "${EUID}" -ne 0 ]]; then
    echo "[info] Not root — re-executing via: sudo -E bash $0 $*"
    exec sudo -E bash "$0" "$@"
fi

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_ts()   { date '+%Y-%m-%dT%H:%M:%S%z'; }
_log()  { echo "[$(_ts)] [backup-loop] $*"; }
_good() { echo "[$(_ts)] <good> $*"; }
_bad()  { echo "[$(_ts)] <bad>  $*"; }
_info() { echo "[$(_ts)] <info> $*"; }

_info "RPO=${RPO_MINUTES}min  transfer=${TRANSFER_ENABLE:-0}  target=${RECOVERY_RSYNC_TARGETS:-<none>}"
_info "Orchestrator: ${ORCHESTRATOR}"
_info "Env:          ${BACKUP_ENV}"
_info "Chain init:   ${CHAIN_VERSION_INIT:-chain-v1}"
echo ""

# ---------------------------------------------------------------------------
# Graceful shutdown on Ctrl+C / SIGTERM
# ---------------------------------------------------------------------------
_cleanup() {
    echo ""
    _info "Received stop signal — backup loop exiting cleanly."
    exit 0
}
trap _cleanup INT TERM

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
consecutive_fails=0
cycle_num=0

while true; do
    cycle_num=$(( cycle_num + 1 ))
    _info "──────────────────────────────────────────────────────"
    _info "Cycle #${cycle_num} starting"

    # Run the orchestrator.
    # -E preserves all sourced env vars through sudo.
    # Redirect stderr to stdout so the log is a single stream.
    if python3 "${ORCHESTRATOR}" 2>&1; then
        consecutive_fails=0
        _good "Cycle #${cycle_num} completed OK"
    else
        consecutive_fails=$(( consecutive_fails + 1 ))
        _bad "Cycle #${cycle_num} FAILED (consecutive_fails=${consecutive_fails}/${MAX_CONSECUTIVE_FAILS})"

        if (( consecutive_fails >= MAX_CONSECUTIVE_FAILS )); then
            _bad "Too many consecutive failures — aborting backup loop."
            exit 2
        fi
    fi

    # Sleep until next cycle.
    sleep_sec=$(( RPO_MINUTES * 60 ))
    _info "Sleeping ${RPO_MINUTES}m until next cycle …"
    echo ""
    sleep "${sleep_sec}"
done
