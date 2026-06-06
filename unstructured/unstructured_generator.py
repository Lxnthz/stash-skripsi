#!/usr/bin/env python3
"""
Unstructured Data Generator for 7-hour DR simulation

Constraints:
- Keeps total data footprint under 2GB
- Uses only standard libraries
- Targets mounted directories:
  - /data/unstructured
  - /monitor

Behavior:
- Phase 1: create base files if target empty
- Phase 2: every 2 minutes perform Append + In-place Edit; rotate every 60 minutes
- Phase 3: log each action to /monitor/unstructured_growth.csv

Run inside a lightweight Alpine-based container.
"""

import os
import time
import random
import string
import shutil
from datetime import datetime, timezone, timedelta

# Configuration
TARGET_DIR = '/data/unstructured'
MONITOR_DIR = '/monitor'
CSV_PATH = os.path.join(MONITOR_DIR, 'unstructured_growth.csv')

# Limits
MAX_TOTAL_BYTES = 2 * 1024**3  # 2GB cap for data footprint
SAFETY_MARGIN = 100 * 1024 * 1024  # keep 100MB headroom

# Base-file sizes (defaults chosen to create >7 actively-changing files)
LOG_COUNT = int(os.environ.get('LOG_COUNT', '20'))
LOG_SIZE_BYTES = int(os.environ.get('LOG_SIZE_BYTES', str(50 * 1024 * 1024)))  # 50MB each

# Keep a couple of large binary-ish files for mid-file edits.
BIN_FILES = [
    ('vm_state.bin', int(os.environ.get('BIN_VM_STATE_BYTES', str(100 * 1024 * 1024)))),
    ('db_index.dat', int(os.environ.get('BIN_DB_INDEX_BYTES', str(100 * 1024 * 1024)))),
]

# Runtime action sizes
APPEND_BYTES_PER_FILE = int(os.environ.get('APPEND_BYTES_PER_FILE', str(50 * 1024)))  # 50KB per file
INPLACE_EDIT_BYTES = int(os.environ.get('INPLACE_EDIT_BYTES', str(16 * 1024)))  # 16KB overwrite

# Per-loop activity knobs
LOGS_TOUCHED_PER_LOOP = int(os.environ.get('LOGS_TOUCHED_PER_LOOP', '6'))
BIN_EDITS_PER_LOOP = int(os.environ.get('BIN_EDITS_PER_LOOP', '2'))

# Rotation policy
ROTATION_INTERVAL_MINUTES = 60
ROTATION_RETENTION = 3  # keep latest N rotated files per app

AVG_LOG_LINE = 120  # approximate bytes per log line (with timestamp)


def env_float(name: str, default: float = 0.0) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except Exception:
        return default


# Chaos: small mid-file corruption (silent bit-flip). Default OFF.
CHAOS_BITFLIP_RATE = env_float('CHAOS_BITFLIP_RATE', 0.0)
CHAOS_BITFLIP_MIN_OFFSET = int(os.environ.get('CHAOS_BITFLIP_MIN_OFFSET', '4096'))


JAKARTA_TZ = timezone(timedelta(hours=7))


def now_iso_jakarta() -> str:
    # Fixed offset is sufficient (Asia/Jakarta has no DST).
    return datetime.now(JAKARTA_TZ).replace(microsecond=0).isoformat()


def ensure_dirs():
    os.makedirs(TARGET_DIR, exist_ok=True)
    os.makedirs(MONITOR_DIR, exist_ok=True)


def dir_total_bytes(path):
    total = 0
    for root, dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def write_csv_header():
    header = 'timestamp,target,action,status,target_file,bytes_changed,total_bytes,total_files,details\n'
    if os.path.exists(CSV_PATH):
        try:
            with open(CSV_PATH, 'r', encoding='utf-8') as f:
                first = f.readline()
            if first.strip() == header.strip():
                return
            # Existing file has an old header/schema; rotate it so new columns apply.
            bak = CSV_PATH + ".bak." + datetime.now(JAKARTA_TZ).strftime('%Y%m%d_%H%M%S')
            os.rename(CSV_PATH, bak)
        except Exception:
            # If we can't read/rotate, fall through and attempt to overwrite.
            pass

    with open(CSV_PATH, 'w', encoding='utf-8') as f:
        f.write(header)


def _dir_stats(path: str) -> tuple[int, int]:
    total_bytes = 0
    total_files = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            total_files += 1
            try:
                total_bytes += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total_bytes, total_files


def log_action(action, target_file, bytes_changed, *, status: str = 'ok', details: str = ''):
    ts = now_iso_jakarta()
    total_bytes, total_files = _dir_stats(TARGET_DIR)
    # Avoid commas in details to keep CSV simple.
    safe_details = (details or '').replace('\n', ' ').replace(',', ';')
    row = f"{ts},unstructured,{action},{status},{target_file},{bytes_changed},{total_bytes},{total_files},{safe_details}\n"
    try:
        with open(CSV_PATH, 'a', encoding='utf-8') as f:
            f.write(row)
    except Exception:
        # best-effort, don't crash the generator
        pass


def make_log_file(path, target_size):
    # Write timestamped lines in chunks until target_size reached
    with open(path, 'w', encoding='utf-8') as f:
        written = 0
        while written < target_size:
            line = f"{now_iso_jakarta()} INFO {random.choice(['worker','api','db','cache'])} msg='{random.choice(['ok','tick','noop','proc'])}' details={random.randint(0,9999999)}\n"
            f.write(line)
            written += len(line.encode('utf-8'))
            # flush occasionally
            if written % (1024*1024) < len(line):
                f.flush()


def make_bin_file(path, target_size):
    # Create compressible but trackable content: repeating blocks with a small unique token
    block = ("BLOCK-" + ''.join(random.choices(string.ascii_letters + string.digits, k=16)) + "-\n").encode('utf-8')
    blk_len = len(block)
    with open(path, 'wb') as f:
        written = 0
        while written < target_size:
            # write repeated block to encourage compression
            to_write = min(blk_len, target_size - written)
            f.write(block[:to_write])
            written += to_write
            if written % (1024*1024) < blk_len:
                f.flush()


def initialize_base_state():
    # If directory is empty, do a full init. If not, expand it up to the configured baseline.
    existing = set()
    try:
        existing = {e.name for e in os.scandir(TARGET_DIR)}
    except Exception:
        existing = set()

    need_full = len(existing) == 0
    if need_full:
        print(f"[{now_iso_jakarta()}] Initializing base state in {TARGET_DIR}")
    else:
        print(f"[{now_iso_jakarta()}] Expanding base state in {TARGET_DIR}")

    # Ensure logs exist up to LOG_COUNT
    created_logs = 0
    for i in range(1, LOG_COUNT + 1):
        fname = f"app_{i:02d}.log"
        if fname in existing:
            continue
        fpath = os.path.join(TARGET_DIR, fname)
        make_log_file(fpath, LOG_SIZE_BYTES)
        created_logs += 1

    # Ensure binary/config files exist
    created_bins = 0
    for name, size in BIN_FILES:
        if name in existing:
            continue
        make_bin_file(os.path.join(TARGET_DIR, name), size)
        created_bins += 1

    if need_full:
        print(f"[{now_iso_jakarta()}] Base state created")
    else:
        print(f"[{now_iso_jakarta()}] Base state ensured: logs_created={created_logs} bins_created={created_bins}")


def safe_append(file_path, bytes_to_append):
    # ensure cap won't be exceeded
    current = dir_total_bytes(TARGET_DIR)
    if current + bytes_to_append + SAFETY_MARGIN > MAX_TOTAL_BYTES:
        # skip to prevent bloat
        log_action('append', os.path.basename(file_path), 0, status='skip', details='space_cap')
        return 0

    lines_needed = max(1, bytes_to_append // AVG_LOG_LINE)
    written = 0
    try:
        with open(file_path, 'a', encoding='utf-8') as f:
            for _ in range(lines_needed):
                line = f"{now_iso_jakarta()} APPEND {random.randint(0,99999)} message='auto'\n"
                f.write(line)
                written += len(line.encode('utf-8'))
            f.flush()
    except Exception as e:
        log_action('append', os.path.basename(file_path), 0, status='fail', details=str(e))
        return 0

    log_action('append', os.path.basename(file_path), written, status='ok')
    return written


def inplace_edit(file_path, edit_bytes=INPLACE_EDIT_BYTES):
    try:
        size = os.path.getsize(file_path)
        if size < edit_bytes + 64:
            offset = 0
        else:
            offset = random.randint(size // 4, (3 * size) // 4)
            if offset + edit_bytes > size:
                offset = max(0, size - edit_bytes - 1)

        token = f"---MODIFIED_FRAG_{now_iso_jakarta()}---"
        payload = (token * ((edit_bytes // len(token)) + 1)).encode('utf-8')[:edit_bytes]
        with open(file_path, 'r+b') as f:
            f.seek(offset)
            f.write(payload)
            f.flush()

        log_action('in_place_edit', os.path.basename(file_path), len(payload), status='ok')
        return len(payload)
    except Exception as e:
        log_action('in_place_edit', os.path.basename(file_path), 0, status='fail', details=str(e))
        return 0


def rotate_log(base_name):
    src = os.path.join(TARGET_DIR, base_name)
    if not os.path.exists(src):
        return 0
    ts = datetime.now(JAKARTA_TZ).strftime('%Y%m%d_%H%M%S')
    dst = os.path.join(TARGET_DIR, f"{base_name}.old.{ts}")
    try:
        os.rename(src, dst)
        # create fresh file
        open(src, 'w', encoding='utf-8').close()
        # retention: keep only latest ROTATION_RETENTION
        old_prefix = f"{base_name}.old"
        old_files = sorted([p for p in os.listdir(TARGET_DIR) if p.startswith(old_prefix)], reverse=True)
        for extra in old_files[ROTATION_RETENTION:]:
            try:
                os.remove(os.path.join(TARGET_DIR, extra))
            except Exception:
                pass
        log_action('rotate', base_name, os.path.getsize(dst) if os.path.exists(dst) else 0, status='ok')
        return 1
    except Exception as e:
        log_action('rotate', base_name, 0, status='fail', details=str(e))
        return 0


def choose_logs_for_append():
    logs = [f for f in os.listdir(TARGET_DIR) if f.endswith('.log') and not f.endswith('.old')]
    if not logs:
        return []
    k = max(1, int(LOGS_TOUCHED_PER_LOOP))
    return random.sample(logs, min(k, len(logs)))


def choose_any_file_for_bitflip():
    candidates = []
    for root, _dirs, files in os.walk(TARGET_DIR):
        for fname in files:
            fpath = os.path.join(root, fname)
            try:
                size = os.path.getsize(fpath)
            except Exception:
                continue
            if size < (CHAOS_BITFLIP_MIN_OFFSET * 2 + 1):
                continue
            candidates.append((fpath, size))
    if not candidates:
        return None
    return random.choice(candidates)


def bitflip_file(file_path, size):
    # Flip a single byte, avoiding the first/last N bytes to prevent "header smash".
    try:
        min_off = min(max(CHAOS_BITFLIP_MIN_OFFSET, 0), max(size - 2, 0))
        max_off = max(min_off, size - min_off - 1)
        if max_off <= min_off:
            return 0
        offset = random.randint(min_off, max_off)

        with open(file_path, 'r+b') as f:
            f.seek(offset)
            b = f.read(1)
            if not b:
                return 0
            flipped = bytes([b[0] ^ 0x01])
            f.seek(offset)
            f.write(flipped)
            f.flush()

        log_action('bitflip', os.path.relpath(file_path, TARGET_DIR), 1, status='ok')
        return 1
    except Exception as e:
        log_action('bitflip', os.path.relpath(file_path, TARGET_DIR), 0, status='fail', details=str(e))
        return 0


def main():
    ensure_dirs()
    write_csv_header()
    initialize_base_state()

    start_time = time.time()
    elapsed_minutes = 0
    loop_count = 0
    print(f"[{now_iso_jakarta()}] Entering main loop")
    try:
        while True:
            loop_count += 1
            # Perform Append on a handful of logs
            selected = choose_logs_for_append()
            for fname in selected:
                fpath = os.path.join(TARGET_DIR, fname)
                safe_append(fpath, APPEND_BYTES_PER_FILE)

            # Perform multiple in-place edits across the binary files
            bin_names = [b[0] for b in BIN_FILES]
            for _ in range(max(1, int(BIN_EDITS_PER_LOOP))):
                bin_choice = random.choice(bin_names)
                inplace_edit(os.path.join(TARGET_DIR, bin_choice), INPLACE_EDIT_BYTES)

            # Optional silent corruption: small mid-file bitflip on any file (including fixtures)
            if CHAOS_BITFLIP_RATE > 0 and random.random() < CHAOS_BITFLIP_RATE:
                picked = choose_any_file_for_bitflip()
                if picked:
                    fpath, size = picked
                    bitflip_file(fpath, size)

            # Rotation every ROTATION_INTERVAL_MINUTES
            elapsed_minutes = int((time.time() - start_time) // 60)
            if elapsed_minutes > 0 and (elapsed_minutes % ROTATION_INTERVAL_MINUTES == 0):
                # rotate app_01.log
                rotate_log('app_01.log')

            # safety check: stop if footprint near limit
            total = dir_total_bytes(TARGET_DIR)
            if total + SAFETY_MARGIN >= MAX_TOTAL_BYTES:
                # log and sleep longer to avoid growth
                log_action('space_limit_reached', '', 0, status='skip', details='space_cap')
                time.sleep(120)
                continue

            time.sleep(120)  # 2 minutes
    except KeyboardInterrupt:
        print(f"[{now_iso_jakarta()}] Stopping on user interrupt")


if __name__ == '__main__':
    main()
