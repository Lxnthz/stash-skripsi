#!/usr/bin/env python3
"""VM2 recovery sender – chain-aware restore.

VM1 requests recovery with:
  --version <chain-v>          which chain to restore from (e.g. chain-v1)
  --chain   <N>                how many cycles to restore (most-recent N in that chain)
  --source  local|cloud|immutable  where to read from
  --send-to-vm1                rsync results back to VM1

Contract
--------
Cycles inside a chain are ordered lexicographically by cycle_id (YYYYMMDD_HHMMSS).
"--chain 3" returns the first 3 cycles in that chain (oldest-first), which is the
correct incremental replay order.

Restore sources
---------------
  local      – read raw cycles from permanent/<chain-v>/<cycle_id>/ (no decryption needed)
  cloud      – download encrypted artifacts from the general GCS bucket via rclone
  immutable  – download encrypted artifacts from the immutable GCS bucket via rclone

Output
------
  Cycles are staged to: <outgoing_root>/<chain-v>/<cycle_id>/...
  Then optionally rsync-pushed to VM1.

"""

from __future__ import annotations

import argparse
import base64
import binascii
import datetime as _dt
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Optional

_THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_THIS_DIR))

from lib.decrypt_aesgcm_b64 import decrypt_b64_aes256gcm_to_file  # noqa: E402
from lib.fsutil import ensure_dir_copy_atomic, ensure_dirs  # noqa: E402


DEFAULT_PERMANENT_ROOT = "/home/recovery/local-backup/permanent"
DEFAULT_ENCRYPTED_ROOT = "/home/recovery/local-backup/encrypted"
DEFAULT_OUTGOING_ROOT = "/home/recovery/local-backup/outgoing/primary"
DEFAULT_VM1_DEST_ROOT = "/home/primary/data/backup-incoming"

ENV_RCLONE_GENERAL_REMOTE = "VM2_RCLONE_GENERAL_REMOTE"
ENV_RCLONE_IMMUTABLE_REMOTE = "VM2_RCLONE_IMMUTABLE_REMOTE"


def _log(msg: str) -> None:
    ts = _dt.datetime.now(tz=_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] {msg}", flush=True)


def _decode_key_from_env() -> bytes:
    key_b64 = os.environ.get("VM2_AES_KEY_B64")
    if not key_b64:
        raise ValueError("VM2_AES_KEY_B64 is required for encrypted-source recovery")
    try:
        key = base64.b64decode(key_b64, validate=True)
    except binascii.Error as exc:
        raise ValueError("VM2_AES_KEY_B64 must be valid Base64") from exc
    if len(key) != 32:
        raise ValueError("VM2_AES_KEY_B64 must decode to exactly 32 bytes")
    return key


def _verify_cycle_ready(cycle_dir: Path) -> None:
    required = [cycle_dir / "manifest.json", cycle_dir / "checksums.sha256"]
    for p in required:
        if not p.is_file():
            raise RuntimeError(f"cycle missing required file: {p}")

    proc = subprocess.run(
        ["sha256sum", "-c", "checksums.sha256"],
        cwd=str(cycle_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"checksums failed for {cycle_dir.name}: {proc.stdout}")


def _list_permanent_cycles(permanent_root: Path, chain_v: str) -> list[str]:
    """Return sorted cycle_ids available in permanent/<chain-v>/."""
    chain_dir = permanent_root / chain_v
    if not chain_dir.is_dir():
        return []
    return sorted(
        p.name
        for p in chain_dir.iterdir()
        if p.is_dir() and not p.name.startswith(".")
    )


def _list_encrypted_cycles(encrypted_root: Path, chain_v: str) -> list[str]:
    """Return sorted cycle_ids available from encrypted/<chain-v>/*.meta.json."""
    chain_dir = encrypted_root / chain_v
    if not chain_dir.is_dir():
        return []
    ids: list[str] = []
    for meta in chain_dir.glob("*.tar.aes256gcm.meta.json"):
        try:
            obj = json.loads(meta.read_text(encoding="utf-8"))
            cid = obj.get("cycle_id")
            if isinstance(cid, str) and cid:
                ids.append(cid)
        except Exception:
            continue
    return sorted(ids)


def _require_rclone() -> None:
    if shutil.which("rclone") is None:
        raise RuntimeError("rclone not found; required for cloud restore sources")


def _rclone_join(remote_base: str, leaf: str) -> str:
    return remote_base.rstrip("/") + "/" + leaf


def _rclone_copyto(remote_file: str, local_path: Path) -> None:
    local_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["rclone", "copyto", remote_file, str(local_path)]
    proc = subprocess.run(cmd, cwd="/", stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"rclone copyto failed: {proc.stdout}")


def _rclone_list_meta(remote_base: str, chain_v: str) -> list[str]:
    """List *.tar.aes256gcm.meta.json files in remote_base/<chain-v>/ and parse cycle_ids."""
    _require_rclone()
    remote_chain = _rclone_join(remote_base, chain_v)
    cmd = ["rclone", "lsjson", remote_chain, "--include", "*.tar.aes256gcm.meta.json"]
    proc = subprocess.run(cmd, cwd="/", stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"rclone lsjson failed: {proc.stdout}")
    try:
        entries = json.loads(proc.stdout)
    except Exception:
        return []

    ids: list[str] = []
    for entry in entries:
        name = entry.get("Name", "")
        if name.endswith(".tar.aes256gcm.meta.json"):
            # Download and parse to get cycle_id.
            with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
                tf_path = Path(tf.name)
            try:
                remote_meta = _rclone_join(remote_chain, name)
                _rclone_copyto(remote_meta, tf_path)
                obj = json.loads(tf_path.read_text(encoding="utf-8"))
                cid = obj.get("cycle_id")
                if isinstance(cid, str) and cid:
                    ids.append(cid)
            except Exception:
                continue
            finally:
                tf_path.unlink(missing_ok=True)
    return sorted(ids)


def _rsync_send_dir(src_dir: Path, dest_root: str, chain_v: str, cycle_id: str) -> None:
    """rsync src_dir → dest_root/<chain-v>/<cycle_id>/"""
    dest = dest_root.rstrip("/") + "/" + chain_v + "/" + cycle_id + "/"
    cmd = ["rsync", "-a", str(src_dir) + "/", dest]
    proc = subprocess.run(cmd, cwd="/", stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"rsync failed for {chain_v}/{cycle_id}: {proc.stdout}")


def _extract_tar(tar_path: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar_path, mode="r") as tf:
        members = tf.getmembers()
        for m in members:
            name = m.name
            if name.startswith("/") or name.startswith("\\"):
                raise RuntimeError(f"unsafe tar member (absolute path): {name}")
            parts = [p for p in name.split("/") if p and p != "."]
            if any(p == ".." for p in parts):
                raise RuntimeError(f"unsafe tar member (path traversal): {name}")
        tf.extractall(path=out_dir, members=members)


def _restore_from_local_encrypted(
    *,
    key: bytes,
    encrypted_root: Path,
    chain_v: str,
    cycle_id: str,
    outgoing_root: Path,
) -> Path:
    """Decrypt local encrypted artifact → outgoing/<chain-v>/<cycle_id>/"""
    b64_path = encrypted_root / chain_v / f"{cycle_id}.tar.aes256gcm.b64"
    if not b64_path.is_file():
        raise RuntimeError(f"missing encrypted artifact: {b64_path}")

    chain_outgoing = outgoing_root / chain_v
    chain_outgoing.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix=f"vm2-recover-{cycle_id}-") as td:
        tar_path = Path(td) / f"{cycle_id}.tar"
        decrypt_b64_aes256gcm_to_file(key=key, in_b64_path=b64_path, out_path=tar_path)
        # Tar contains chain-v/cycle_id/ at top level; extract into outgoing_root.
        _extract_tar(tar_path, outgoing_root)

    restored = chain_outgoing / cycle_id
    if not restored.is_dir():
        raise RuntimeError(f"expected restored cycle dir not found: {restored}")
    _verify_cycle_ready(restored)
    return restored


def _restore_from_cloud(
    *,
    key: bytes,
    rclone_remote: str,
    chain_v: str,
    cycle_id: str,
    outgoing_root: Path,
) -> Path:
    """Download + decrypt encrypted artifact from cloud → outgoing/<chain-v>/<cycle_id>/"""
    _require_rclone()
    chain_outgoing = outgoing_root / chain_v
    chain_outgoing.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix=f"vm2-cloud-{cycle_id}-") as td:
        td_path = Path(td)
        b64_leaf = f"{cycle_id}.tar.aes256gcm.b64"
        remote_b64 = _rclone_join(_rclone_join(rclone_remote, chain_v), b64_leaf)
        local_b64 = td_path / b64_leaf
        _rclone_copyto(remote_b64, local_b64)

        tar_path = td_path / f"{cycle_id}.tar"
        decrypt_b64_aes256gcm_to_file(key=key, in_b64_path=local_b64, out_path=tar_path)
        _extract_tar(tar_path, outgoing_root)

    restored = chain_outgoing / cycle_id
    if not restored.is_dir():
        raise RuntimeError(f"expected restored cycle dir not found: {restored}")
    _verify_cycle_ready(restored)
    return restored


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="VM2 recovery sender (chain-aware)")
    p.add_argument(
        "--version",
        required=True,
        dest="chain_v",
        metavar="CHAIN_V",
        help="chain version to restore from (e.g. chain-v1)",
    )
    p.add_argument(
        "--chain",
        required=True,
        type=int,
        dest="num_cycles",
        metavar="N",
        help="number of cycles to restore (first N in lexicographic order, i.e. oldest N)",
    )
    p.add_argument(
        "--source",
        choices=["local", "cloud", "immutable"],
        default="local",
        help="where to read cycles from: local (permanent store), cloud (general bucket), immutable (immutable bucket). Default: local",
    )
    p.add_argument("--permanent-root", default=os.environ.get("VM2_PERMANENT_ROOT", DEFAULT_PERMANENT_ROOT))
    p.add_argument("--encrypted-root", default=os.environ.get("VM2_ENCRYPTED_ROOT", DEFAULT_ENCRYPTED_ROOT))
    p.add_argument("--outgoing-root", default=os.environ.get("VM2_OUTGOING_ROOT", DEFAULT_OUTGOING_ROOT))
    p.add_argument(
        "--rclone-remote",
        default=None,
        help="override rclone remote:path base (otherwise uses env VM2_RCLONE_GENERAL_REMOTE / VM2_RCLONE_IMMUTABLE_REMOTE)",
    )
    p.add_argument(
        "--send-to-vm1",
        action="store_true",
        help="rsync-push cycles to VM1 destination root after staging",
    )
    p.add_argument(
        "--vm1-dest-root",
        default=os.environ.get("VM1_RESTORE_DEST_ROOT", DEFAULT_VM1_DEST_ROOT),
        help=f"VM1 receive root (default: env VM1_RESTORE_DEST_ROOT or {DEFAULT_VM1_DEST_ROOT})",
    )
    p.add_argument("--dry-run", action="store_true", help="print selected cycles and exit without restoring")

    args = p.parse_args(argv)

    chain_v: str = args.chain_v
    num_cycles: int = args.num_cycles
    source: str = args.source

    if num_cycles < 1:
        _log("--chain must be >= 1")
        return 2

    permanent_root = Path(args.permanent_root)
    encrypted_root = Path(args.encrypted_root)
    outgoing_root = Path(args.outgoing_root)
    ensure_dirs(outgoing_root / chain_v)

    # ---------- resolve available cycle_ids ----------
    if source == "local":
        all_ids = _list_permanent_cycles(permanent_root, chain_v)
        if not all_ids:
            _log(f"no cycles found under permanent root {permanent_root / chain_v}")
            return 4
    elif source in ("cloud", "immutable"):
        env_name = ENV_RCLONE_GENERAL_REMOTE if source == "cloud" else ENV_RCLONE_IMMUTABLE_REMOTE
        remote = args.rclone_remote or os.environ.get(env_name)
        if not remote:
            _log(f"missing required env var {env_name} (rclone remote:path)")
            return 2
        _log(f"listing cycles from {source} bucket ({remote}/{chain_v}) ...")
        try:
            all_ids = _rclone_list_meta(remote, chain_v)
        except RuntimeError as exc:
            _log(f"failed to list cloud cycles: {exc}")
            return 4
        if not all_ids:
            _log(f"no cycle meta found in {source} bucket under {chain_v}/")
            return 4
    else:
        _log(f"unknown source: {source}")
        return 2

    # Select first N cycles (oldest-first = correct incremental replay order).
    selected_ids = all_ids[:num_cycles]

    if args.dry_run:
        _log(f"dry-run: chain={chain_v}, source={source}, available={len(all_ids)}, selected={len(selected_ids)}")
        for cid in selected_ids:
            print(cid)
        return 0

    # ---------- resolve decryption key if needed ----------
    if source in ("cloud", "immutable"):
        try:
            key = _decode_key_from_env()
        except ValueError as exc:
            _log(str(exc))
            return 2
        rclone_remote = args.rclone_remote or os.environ.get(
            ENV_RCLONE_GENERAL_REMOTE if source == "cloud" else ENV_RCLONE_IMMUTABLE_REMOTE
        )
    else:
        key = b""
        rclone_remote = None

    _log(f"chain={chain_v}  source={source}  selected={len(selected_ids)} cycles")

    # ---------- restore each cycle ----------
    for cid in selected_ids:
        _log(f"  restoring {chain_v}/{cid} ...")

        if source == "local":
            src_cycle = permanent_root / chain_v / cid
            _verify_cycle_ready(src_cycle)
            dest = outgoing_root / chain_v / cid
            ensure_dir_copy_atomic(src_cycle, dest)

        elif source in ("cloud", "immutable"):
            _restore_from_cloud(
                key=key,
                rclone_remote=rclone_remote,
                chain_v=chain_v,
                cycle_id=cid,
                outgoing_root=outgoing_root,
            )

        if args.send_to_vm1:
            staged = outgoing_root / chain_v / cid
            _rsync_send_dir(staged, args.vm1_dest_root, chain_v, cid)

    _log(f"recovery export complete: {len(selected_ids)} cycle(s) from {chain_v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
