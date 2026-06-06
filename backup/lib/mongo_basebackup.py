import os
import subprocess
import time

from .state import get_metadata, set_metadata


def _fmt_elapsed(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.2f}s"
    mins = int(seconds // 60)
    secs = seconds - mins * 60
    return f"{mins}m{secs:.1f}s"


def _should_take_basebackup(*, conn_state, key_cycle: str, every_n: int) -> bool:
    last = get_metadata(conn_state, key_cycle)
    if not last:
        return True
    if every_n and every_n > 0:
        try:
            last_n = int(get_metadata(conn_state, key_cycle + ":n") or "0")
            cur_n = int(get_metadata(conn_state, "cycle_num") or "0")
            return (cur_n - last_n) >= int(every_n)
        except Exception:
            return False
    return False


def take_mongo_basebackup_into_cycle(
    *,
    conn_state,
    cycle_id: str,
    cycle_tmp: str,
    mongo_docker_container: str,
    mongo_uri: str,
    db_name: str,
    include_oplog: bool,
) -> dict | None:
    """Create a mongodump archive (gzip) and place it into cycle_tmp/mongo/basebackup/mongodump.archive.gz."""

    started = time.perf_counter()

    out_dir = os.path.join(cycle_tmp, "mongo", "basebackup")
    os.makedirs(out_dir, exist_ok=True)

    out_path = os.path.join(out_dir, "mongodump.archive.gz")
    rel_artifact = os.path.relpath(out_path, cycle_tmp)

    # NOTE: mongodump --oplog is only supported for full dumps (no --db / no --collection).
    # If oplog is enabled, ignore db_name and perform a full dump.
    effective_db = (db_name or "").strip()
    if include_oplog and effective_db:
        print(f"[MONGO][BASE] Note: ignoring db={effective_db} because --oplog requires full dump")
        effective_db = ""

    cmd = [
        "docker",
        "exec",
        "-i",
        mongo_docker_container,
        "mongodump",
        f"--uri={mongo_uri}",
        "--archive",
        "--gzip",
    ]
    if effective_db:
        cmd.append(f"--db={effective_db}")
    if include_oplog:
        cmd.append("--oplog")

    db_label = effective_db if effective_db else "<all>"
    print(f"[MONGO][BASE] Starting mongodump (db={db_label}, oplog={bool(include_oplog)}) -> {rel_artifact}")

    tmp_path = out_path + ".tmp"
    try:
        with open(tmp_path, "wb") as f:
            subprocess.run(cmd, stdout=f, check=True)
        os.replace(tmp_path, out_path)
    except Exception as e:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        print(f"[MONGO][BASE] FAIL: mongodump failed: {e}")
        return None

    try:
        stored_bytes = int(os.path.getsize(out_path))
    except Exception:
        stored_bytes = 0

    elapsed = time.perf_counter() - started
    print(f"[MONGO][BASE] Done: stored_bytes={stored_bytes} elapsed={_fmt_elapsed(elapsed)}")

    set_metadata(conn_state, "mongo_last_basebackup_cycle", cycle_id)
    try:
        set_metadata(conn_state, "mongo_last_basebackup_cycle:n", str(int(get_metadata(conn_state, "cycle_num") or "0")))
    except Exception:
        pass

    return {
        "artifact": rel_artifact,
        "format": "mongodump-archive+gzip",
        "db": db_name,
        "oplog": bool(include_oplog),
        "stored_bytes": stored_bytes,
    }


def maybe_take_mongo_basebackup(*, cfg, conn_state, cycle_id: str, cycle_tmp: str, mongo_uri: str) -> dict | None:
    if not getattr(cfg, "mongo_basebackup_enable", False):
        print("[MONGO][BASE] Disabled (MONGO_BASEBACKUP_ENABLE=0)")
        return None

    if getattr(cfg, "mongo_basebackup_force", False):
        print("[MONGO][BASE] Forced (MONGO_BASEBACKUP_FORCE=1)")
        return take_mongo_basebackup_into_cycle(
            conn_state=conn_state,
            cycle_id=cycle_id,
            cycle_tmp=cycle_tmp,
            mongo_docker_container=cfg.mongo_docker_container,
            mongo_uri=mongo_uri,
            db_name=cfg.mongo_basebackup_db,
            include_oplog=bool(cfg.mongo_basebackup_oplog),
        )

    if not _should_take_basebackup(
        conn_state=conn_state,
        key_cycle="mongo_last_basebackup_cycle",
        every_n=int(getattr(cfg, "mongo_basebackup_every_n_cycles", 0) or 0),
    ):
        try:
            cur_n = int(get_metadata(conn_state, "cycle_num") or "0")
            last_n = int(get_metadata(conn_state, "mongo_last_basebackup_cycle:n") or "0")
            every_n = int(getattr(cfg, "mongo_basebackup_every_n_cycles", 0) or 0)
            print(f"[MONGO][BASE] Skipped (not due yet): cycle_num={cur_n} last_base_n={last_n} every_n={every_n}")
        except Exception:
            print("[MONGO][BASE] Skipped (not due yet)")
        return None

    return take_mongo_basebackup_into_cycle(
        conn_state=conn_state,
        cycle_id=cycle_id,
        cycle_tmp=cycle_tmp,
        mongo_docker_container=cfg.mongo_docker_container,
        mongo_uri=mongo_uri,
        db_name=cfg.mongo_basebackup_db,
        include_oplog=bool(cfg.mongo_basebackup_oplog),
    )


__all__ = ["maybe_take_mongo_basebackup"]
