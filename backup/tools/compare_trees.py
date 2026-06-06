#!/usr/bin/env python3

import argparse
import hashlib
import os
from dataclasses import dataclass


def _log(msg: str) -> None:
    print(f"[COMPARE] {msg}")


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _walk_files(root: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for walk_root, _dirs, files in os.walk(root):
        for name in files:
            p = os.path.join(walk_root, name)
            rel = os.path.relpath(p, root)
            out[rel] = p
    return out


@dataclass
class CompareResult:
    ok: bool
    same: int
    different: int
    missing_left: int
    missing_right: int


def compare_dirs(*, left: str, right: str, max_diffs: int = 20) -> CompareResult:
    left = os.path.abspath(left)
    right = os.path.abspath(right)

    if not os.path.isdir(left):
        raise NotADirectoryError(left)
    if not os.path.isdir(right):
        raise NotADirectoryError(right)

    _log(f"Left:  {left}")
    _log(f"Right: {right}")

    left_files = _walk_files(left)
    right_files = _walk_files(right)

    left_keys = set(left_files.keys())
    right_keys = set(right_files.keys())

    missing_left = sorted(right_keys - left_keys)
    missing_right = sorted(left_keys - right_keys)

    if missing_left:
        _log(f"Missing in LEFT: {len(missing_left)}")
        for rel in missing_left[:max_diffs]:
            _log(f"  - {rel}")
        if len(missing_left) > max_diffs:
            _log(f"  ... and {len(missing_left) - max_diffs} more")

    if missing_right:
        _log(f"Missing in RIGHT: {len(missing_right)}")
        for rel in missing_right[:max_diffs]:
            _log(f"  - {rel}")
        if len(missing_right) > max_diffs:
            _log(f"  ... and {len(missing_right) - max_diffs} more")

    same = 0
    different = 0
    diffs_printed = 0

    common = sorted(left_keys & right_keys)
    _log(f"Common files: {len(common)}")

    for rel in common:
        lpath = left_files[rel]
        rpath = right_files[rel]
        lsha = _sha256_file(lpath)
        rsha = _sha256_file(rpath)
        if lsha == rsha:
            same += 1
        else:
            different += 1
            if diffs_printed < max_diffs:
                _log(f"DIFF {rel}: left={lsha} right={rsha}")
                diffs_printed += 1

    ok = (different == 0 and len(missing_left) == 0 and len(missing_right) == 0)
    _log(
        f"Summary: ok={ok} same={same} different={different} missing_left={len(missing_left)} missing_right={len(missing_right)}"
    )
    return CompareResult(
        ok=ok,
        same=same,
        different=different,
        missing_left=len(missing_left),
        missing_right=len(missing_right),
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Compare two directory trees by SHA256 (integrity compliance check).")
    ap.add_argument("--left", required=True, help="Left directory")
    ap.add_argument("--right", required=True, help="Right directory")
    ap.add_argument("--max-diffs", type=int, default=20, help="Max diff lines to print")
    args = ap.parse_args()

    res = compare_dirs(left=args.left, right=args.right, max_diffs=args.max_diffs)
    return 0 if res.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
