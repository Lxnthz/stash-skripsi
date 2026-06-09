#!/usr/bin/env python3

import argparse
import os
import pwd
import shutil
import sys
from pathlib import Path


DEFAULTS = {
    "incoming_dir": "/home/recovery/local-backup/incoming",
    "restore_request_dir": "/home/recovery/local-backup/restore-requests",
    "outgoing_root": "/home/recovery/local-backup/outgoing/primary",
    "state_dir": "/home/recovery/local-backup/state",
    "encrypted_root": "/local-backup/encrypted",
    # work_dir default is derived from encrypted_root parent (matches vm2_cycle_processor.py behavior)
}


def _rm_rf(path: Path) -> None:
    if not path.exists():
        return
    if path.is_symlink() or path.is_file():
        path.unlink()
        return
    shutil.rmtree(path)


def _rm_contents(dir_path: Path) -> None:
    if not dir_path.exists():
        return
    if not dir_path.is_dir():
        _rm_rf(dir_path)
        return
    for child in dir_path.iterdir():
        _rm_rf(child)


def _ensure_dir(path: Path, *, mode: int | None = None, chown: tuple[int, int] | None = None) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if mode is not None:
        os.chmod(path, mode)
    if chown is not None:
        try:
            os.chown(path, chown[0], chown[1])
        except PermissionError:
            # best-effort; some mounts may not allow chown
            pass


def _owner_ids() -> tuple[int, int]:
    owner_user = os.environ.get("SUDO_USER") or os.environ.get("USER") or "recovery"
    try:
        pw = pwd.getpwnam(owner_user)
        return int(pw.pw_uid), int(pw.pw_gid)
    except Exception:
        return 0, 0


def _needs_root(paths: list[Path]) -> bool:
    # Heuristic: if any path is under /local-backup, assume root might be required.
    for p in paths:
        try:
            if str(p).startswith("/local-backup"):
                return True
        except Exception:
            pass
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="Reset VM2 backup VM local state to initial empty state (DESTRUCTIVE).")
    ap.add_argument("--yes", action="store_true", help="Actually perform deletion.")
    ap.add_argument("--incoming-dir", default=DEFAULTS["incoming_dir"])
    ap.add_argument("--restore-request-dir", default=DEFAULTS["restore_request_dir"])
    ap.add_argument("--outgoing-root", default=DEFAULTS["outgoing_root"])
    ap.add_argument("--state-dir", default=DEFAULTS["state_dir"])
    ap.add_argument("--encrypted-root", default=DEFAULTS["encrypted_root"])
    ap.add_argument(
        "--work-dir",
        default=None,
        help="Optional staging/work dir; default: <encrypted-root>/../staging",
    )
    ap.add_argument(
        "--keep-incoming",
        action="store_true",
        help="Do not wipe incoming/ (rare; default is wipe).",
    )
    ap.add_argument(
        "--keep-requests",
        action="store_true",
        help="Do not wipe restore-requests/ (rare; default is wipe).",
    )
    ap.add_argument(
        "--keep-outgoing",
        action="store_true",
        help="Do not wipe outgoing/primary (rare; default is wipe).",
    )

    args = ap.parse_args()

    incoming_dir = Path(args.incoming_dir)
    restore_request_dir = Path(args.restore_request_dir)
    outgoing_root = Path(args.outgoing_root)
    state_dir = Path(args.state_dir)
    encrypted_root = Path(args.encrypted_root)
    work_dir = Path(args.work_dir) if args.work_dir else encrypted_root.parent / "staging"

    targets = [
        incoming_dir,
        restore_request_dir,
        outgoing_root,
        state_dir,
        encrypted_root,
        work_dir,
    ]

    if not args.yes:
        print("Refusing to reset without --yes")
        print("This will DELETE VM2 local backup state:")
        for t in targets:
            print(f"  - {t}")
        print("Recommended: stop the VM2 service first.")
        print("Run (may require sudo if /local-backup is root-owned):")
        print("  sudo python3 <vm2_reset_script>.py --yes")
        return 2

    if _needs_root(targets) and os.geteuid() != 0:
        print("[FAIL] This reset likely requires root (paths under /local-backup).")
        print("Run:")
        print("  sudo python3 <vm2_reset_script>.py --yes")
        return 2

    owner_uid, owner_gid = _owner_ids()

    # Wipe contents (keep top-level dirs)
    if not args.keep_incoming:
        _rm_contents(incoming_dir)
    if not args.keep_requests:
        _rm_contents(restore_request_dir)
    if not args.keep_outgoing:
        _rm_contents(outgoing_root)

    # Hard wipe encrypted/work/state (contents only; keep dirs)
    _rm_contents(encrypted_root)
    _rm_contents(work_dir)
    _rm_contents(state_dir)

    # Recreate required dirs with usable perms for the service user
    for d in [
        incoming_dir,
        restore_request_dir,
        outgoing_root,
        state_dir,
        permanent_root,
        encrypted_root,
        work_dir,
    ]:
        _ensure_dir(d, mode=0o775, chown=(owner_uid, owner_gid))

    print("[OK] VM2 reset complete (local only).")
    print("Next: start the VM2 service and resend cycles if needed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
