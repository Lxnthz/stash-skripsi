#!/usr/bin/env python3

import argparse
import hashlib
import json
import os
import shutil
import zlib


def _log(prefix: str, msg: str) -> None:
    print(f"{prefix} {msg}")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_manifest(cycle_dir: str) -> dict:
    path = os.path.join(cycle_dir, "manifest.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _decompress_zlib(path: str) -> bytes:
    with open(path, "rb") as f:
        payload = f.read()
    return zlib.decompress(payload)


def _apply_chunk(*, out_unstructured_dir: str, rel_file: str, chunk_index: int, chunk_size: int, chunk_bytes: bytes) -> None:
    dst_path = os.path.join(out_unstructured_dir, rel_file)
    os.makedirs(os.path.dirname(dst_path), exist_ok=True)

    offset = int(chunk_index) * int(chunk_size)

    mode = "r+b" if os.path.exists(dst_path) else "w+b"
    with open(dst_path, mode) as f:
        f.seek(0, os.SEEK_END)
        cur_size = f.tell()
        if cur_size < offset:
            f.write(b"\x00" * (offset - cur_size))
        f.seek(offset)
        f.write(chunk_bytes)


def restore_unstructured(*, base_unstructured_dir: str, out_unstructured_dir: str, cycle_dirs: list[str], chunk_size: int) -> dict:
    _log("[RESTORE]", f"Base snapshot: {base_unstructured_dir}")
    _log("[RESTORE]", f"Output dir:    {out_unstructured_dir}")
    _log("[RESTORE]", f"Chunk size:    {chunk_size}")
    _log("[RESTORE]", f"Cycles:        {len(cycle_dirs)}")

    if os.path.exists(out_unstructured_dir):
        shutil.rmtree(out_unstructured_dir, ignore_errors=True)
    shutil.copytree(base_unstructured_dir, out_unstructured_dir)

    applied = 0
    verified_ok = 0
    verified_fail = 0
    files_touched: set[str] = set()

    for cycle_dir in cycle_dirs:
        _log("[RESTORE]", f"Reading manifest: {os.path.join(cycle_dir, 'manifest.json')}")
        manifest = _read_manifest(cycle_dir)
        pfc = manifest.get("pfc") or {}
        deltas = pfc.get("deltas") if isinstance(pfc, dict) else None
        if not isinstance(deltas, list):
            continue

        _log("[PFC]", f"Applying deltas from {cycle_dir}: count={len(deltas)}")

        for entry in deltas:
            if not isinstance(entry, dict):
                continue

            rel_art = entry.get("artifact")
            rel_file = entry.get("source_file")
            chunk_index = entry.get("chunk_index")
            compression = entry.get("compression")
            expected_sha = entry.get("raw_sha256")

            if not rel_art or not rel_file or chunk_index is None:
                continue

            art_path = os.path.join(cycle_dir, rel_art)
            if not os.path.isfile(art_path):
                raise FileNotFoundError(f"Missing PFC artifact: {art_path}")

            if compression == "zlib":
                raw = _decompress_zlib(art_path)
            else:
                with open(art_path, "rb") as f:
                    raw = f.read()

            applied += 1
            files_touched.add(rel_file)

            if expected_sha:
                actual_sha = _sha256(raw)
                if actual_sha == expected_sha:
                    verified_ok += 1
                else:
                    verified_fail += 1
                    _log(
                        "[PFC]",
                        f"HASH MISMATCH file={rel_file} chunk={chunk_index} expected={expected_sha} actual={actual_sha}",
                    )

            _apply_chunk(
                out_unstructured_dir=out_unstructured_dir,
                rel_file=rel_file,
                chunk_index=int(chunk_index),
                chunk_size=int(chunk_size),
                chunk_bytes=raw,
            )

    return {
        "applied_chunks": applied,
        "verified_ok": verified_ok,
        "verified_fail": verified_fail,
        "files_touched": len(files_touched),
        "out_dir": out_unstructured_dir,
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Restore unstructured directory from a base snapshot + PFC deltas from one or more cycles (VM1-only)."
    )
    ap.add_argument("--base-unstructured", required=True, help="Base snapshot directory (copied as starting point)")
    ap.add_argument("--out-unstructured", required=True, help="Output restored directory")
    ap.add_argument(
        "--chunk-size",
        type=int,
        default=1024 * 1024,
        help="Chunk size used by PFC during backup (default 1048576)",
    )
    ap.add_argument("cycles", nargs="+", help="One or more cycle directories (apply in order)")
    args = ap.parse_args()

    stats = restore_unstructured(
        base_unstructured_dir=args.base_unstructured,
        out_unstructured_dir=args.out_unstructured,
        cycle_dirs=args.cycles,
        chunk_size=args.chunk_size,
    )

    _log(
        "[RESTORE]",
        "Done: out={out_dir} files_touched={files_touched} chunks_applied={applied_chunks} verified_ok={verified_ok} verified_fail={verified_fail}".format(
            **stats
        ),
    )

    if int(stats.get("verified_fail", 0)) != 0:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
