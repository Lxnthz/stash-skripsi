import os
import time
from datetime import datetime
from .io_utils import copy_file_stream
from .state import get_metadata

# PostgreSQL WAL segment size is fixed at 16 MiB (default, compiled in).
_WAL_SEGMENT_BYTES = 16 * 1024 * 1024  # 16 MiB


def _is_wal_segment(name: str) -> bool:
    # Typical WAL segment name: 24 hex chars (timeline+log+seg)
    if not name or len(name) != 24:
        return False
    for ch in name:
        if ch not in "0123456789ABCDEF":
            return False
    return True


def _pad_wal_to_full(path: str) -> int:
    """Pad a WAL file at *path* to exactly _WAL_SEGMENT_BYTES with zero bytes.

    Returns the number of bytes added (0 if already full-sized).
    Raises ValueError if the file is larger than expected.
    """
    size = os.path.getsize(path)
    if size == _WAL_SEGMENT_BYTES:
        return 0
    if size > _WAL_SEGMENT_BYTES:
        raise ValueError(
            f"WAL segment {path} is {size} bytes (larger than expected {_WAL_SEGMENT_BYTES})"
        )
    pad = _WAL_SEGMENT_BYTES - size
    with open(path, "ab") as f:
        # Write in 1 MiB chunks to avoid a single huge allocation.
        remaining = pad
        chunk_size = 1024 * 1024
        zero_chunk = b"\x00" * chunk_size
        while remaining >= chunk_size:
            f.write(zero_chunk)
            remaining -= chunk_size
        if remaining:
            f.write(b"\x00" * remaining)
    return pad


def harvest_wals(
    *,
    conn_state,
    pg_wal_archive: str,
    out_dir: str,
    last_cycle_key: str = "last_cycle_ts",
    tzinfo=None,
):
    started = time.perf_counter()
    print("[PG] Starting WAL harvest")

    last_cycle_ts = get_metadata(conn_state, last_cycle_key)
    try:
        last_cycle_ts = int(last_cycle_ts) if last_cycle_ts else 0
    except Exception:
        last_cycle_ts = 0

    last_cycle_iso = None
    if last_cycle_ts:
        try:
            if tzinfo is not None:
                last_cycle_iso = datetime.fromtimestamp(last_cycle_ts, tzinfo).isoformat()
            else:
                last_cycle_iso = datetime.fromtimestamp(last_cycle_ts).isoformat()
        except Exception:
            last_cycle_iso = None
    if last_cycle_iso:
        print(f"[PG] Last cycle ts: {last_cycle_ts} ({last_cycle_iso})")
    else:
        print(f"[PG] Last cycle ts: {last_cycle_ts}")
    print(f"[PG] WAL archive: {pg_wal_archive}")

    last_wal_fname = (get_metadata(conn_state, "pg_last_wal_fname") or "").strip().upper()
    if last_wal_fname:
        print(f"[PG] Last WAL fname: {last_wal_fname}")
    else:
        print("[PG] Last WAL fname: <none>")

    copied = []
    copied_bytes = 0
    skipped = 0
    padded_count = 0
    padded_bytes = 0
    newest_segment_copied = None
    if os.path.isdir(pg_wal_archive):
        for fname in sorted(os.listdir(pg_wal_archive)):
            src = os.path.join(pg_wal_archive, fname)
            if not os.path.isfile(src):
                # Basebackup temp directories (or other non-files) can appear here.
                # Never treat them as WAL artifacts.
                print(f"[PG] Skipping non-file in WAL archive: {src}")
                continue
            try:
                mtime = int(os.path.getmtime(src))
            except Exception:
                continue
            upper = str(fname).upper()
            # For WAL segments, prefer name-based incremental selection.
            if _is_wal_segment(upper) and (not last_wal_fname or upper > last_wal_fname):
                dst = os.path.join(out_dir, fname)
                try:
                    size = int(os.path.getsize(src))
                except Exception:
                    size = 0
                print(f"[PG] Copying WAL {src} -> {dst} ({size} bytes)")
                copy_file_stream(src, dst)

                # --- Feature 1: Pad incomplete WAL to full 16 MiB ---
                if size < _WAL_SEGMENT_BYTES:
                    try:
                        added = _pad_wal_to_full(dst)
                        print(
                            f"[PG] Padded WAL {fname}: {size} -> {size + added} bytes "
                            f"(added {added} zero bytes to reach 16 MiB)"
                        )
                        padded_count += 1
                        padded_bytes += added
                        # Record the padded size for accounting
                        size = _WAL_SEGMENT_BYTES
                    except Exception as pad_err:
                        print(f"[PG] WARNING: could not pad WAL {fname}: {pad_err}")
                elif size > _WAL_SEGMENT_BYTES:
                    print(
                        f"[PG] WARNING: WAL {fname} is {size} bytes (>{_WAL_SEGMENT_BYTES}); "
                        "not a standard 16 MiB segment — copied as-is"
                    )

                copied.append(dst)
                copied_bytes += size
                newest_segment_copied = upper
            elif mtime > last_cycle_ts:
                # Non-segment artifacts (e.g., .backup history) are still copied by mtime.
                dst = os.path.join(out_dir, fname)
                try:
                    size = int(os.path.getsize(src))
                except Exception:
                    size = 0
                print(f"[PG] Copying WAL {src} -> {dst} ({size} bytes)")
                copy_file_stream(src, dst)
                copied.append(dst)
                copied_bytes += size
            else:
                skipped += 1

    elapsed = time.perf_counter() - started
    print(
        f"[PG] WAL summary: copied={len(copied)} files ({copied_bytes} bytes), "
        f"padded={padded_count} segments (+{padded_bytes} bytes), "
        f"skipped={skipped} older-or-equal, elapsed={elapsed:.2f}s"
    )

    # record that we harvested (even if 0) via a monotonic timestamp key; actual state update happens in runner
    return {
        "copied_paths": copied,
        "copied_files": len(copied),
        "copied_bytes": copied_bytes,
        "padded_count": padded_count,
        "padded_bytes": padded_bytes,
        "skipped_files": skipped,
        "last_cycle_ts": last_cycle_ts,
        "newest_segment_copied": newest_segment_copied,
        "elapsed_s": elapsed,
    }


__all__ = ["harvest_wals"]
