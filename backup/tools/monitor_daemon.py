#!/usr/bin/env python3

import argparse
import csv
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta


JAKARTA_TZ = timezone(timedelta(hours=7))


def now_iso_jakarta() -> str:
    return datetime.now(JAKARTA_TZ).replace(microsecond=0).isoformat()


import sys

LOG_FILE = "/home/primary/utilities/backup/backup.log"

def _isatty() -> bool:
    try:
        return sys.stdout.isatty()
    except Exception:
        return False

def _color_enabled() -> bool:
    if os.environ.get("NO_COLOR") is not None:
        return False
    return _isatty() and os.environ.get("LOG_COLOR", "1") != "0"

def _c(s: str, code: str) -> str:
    if not _color_enabled():
        return s
    return f"\x1b[{code}m{s}\x1b[0m"

def good_tag() -> str: return _c("<good>", "32")
def bad_tag() -> str: return _c("<error>", "31")
def info_tag() -> str: return _c("<info>", "36")
def warn_tag() -> str: return _c("<warn>", "33")

def _tag_good() -> str: return good_tag()
def _tag_bad() -> str: return bad_tag()
def _tag_info() -> str: return info_tag()

def _log(scope: str, tag: str, msg: str) -> None:
    # Strip ANSI color codes for file logging
    import re
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    clean_tag = ansi_escape.sub('', tag)
    
    # ensure tidy formatting
    term_line = f"{scope:<10} {tag:<8} {msg}"
    print(term_line)
    
    try:
        # Jakarta tz
        import datetime
        tz = datetime.timezone(datetime.timedelta(hours=7))
        ts = datetime.datetime.now(tz).replace(microsecond=0).isoformat()
        file_line = f"[{ts}] {scope:<10} {clean_tag:<8} {msg}\n"
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(file_line)
    except Exception:
        pass

def _run(cmd: list[str], *, timeout_s: int = 20) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout_s,
        )
        return proc.returncode, proc.stdout
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except Exception as e:
        return 1, str(e)


def ensure_csv_header(path: str, header: list[str]) -> None:
    if os.path.exists(path):
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(header)


def append_csv_row(path: str, row: list[object]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(row)

@dataclass
class Totals:
    a: int
    b: int
    c: int


def _parse_csv3(s: str) -> Totals | None:
    s = (s or "").strip()
    parts = [p.strip() for p in s.split(",")]
    if len(parts) < 3:
        return None
    try:
        return Totals(int(parts[0]), int(parts[1]), int(parts[2]))
    except Exception:
        return None


def _delta(cur: Totals, prev: Totals | None) -> Totals:
    if prev is None:
        return Totals(0, 0, 0)
    # Signed deltas: a TRUNCATE or container reset should show as negative,
    # and a restore jump should show as positive.
    return Totals(cur.a - prev.a, cur.b - prev.b, cur.c - prev.c)


def read_pg_totals(*, container: str) -> tuple[Totals | None, str | None, float]:
    cmd = [
        "docker",
        "exec",
        "-i",
        container,
        "psql",
        "-U",
        "postgresql",
        "-d",
        "transactiondb",
        "-t",
        "-A",
        "-F",
        ",",
        "-c",
        "SELECT (SELECT count(*) FROM customers),(SELECT count(*) FROM products),(SELECT count(*) FROM orders);",
    ]
    start = time.perf_counter()
    rc, out = _run(cmd)
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    if rc != 0:
        return None, out.strip()[-200:], elapsed_ms
    last = [line.strip() for line in out.splitlines() if line.strip()][-1]
    t = _parse_csv3(last)
    if not t:
        return None, f"bad_output:{last[:200]}", elapsed_ms
    return t, None, elapsed_ms


def read_mongo_totals(*, container: str, uri: str) -> tuple[Totals | None, str | None, float]:
    cmd = [
        "docker",
        "exec",
        "-i",
        container,
        "mongosh",
        uri,
        "--quiet",
        "--eval",
        "print(db.products.countDocuments()+','+db.users.countDocuments()+','+db.orders.countDocuments());",
    ]
    start = time.perf_counter()
    rc, out = _run(cmd)
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    if rc != 0:
        return None, out.strip()[-200:], elapsed_ms
    last = [line.strip() for line in out.splitlines() if line.strip()][-1]
    t = _parse_csv3(last)
    if not t:
        return None, f"bad_output:{last[:200]}", elapsed_ms
    return t, None, elapsed_ms


def read_unstructured_totals(*, dir_path: str) -> tuple[int, int, float]:
    start = time.perf_counter()
    file_count = 0
    total_bytes = 0
    try:
        for root, dirs, files in os.walk(dir_path):
            file_count += len(files)
            for f in files:
                try:
                    total_bytes += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
    except OSError:
        pass
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    return file_count, total_bytes, elapsed_ms


def write_resource_row(*, out_csv: str, disk_path: str) -> tuple[bool, str]:
    # Reuse existing resource_monitor implementation by shelling out (keeps logic in one place).
    cmd = [sys.executable, os.path.join(os.path.dirname(__file__), "resource_monitor.py"), "--out", out_csv, "--disk-path", disk_path]
    rc, out = _run(cmd, timeout_s=30)
    if rc == 0:
        return True, out.strip().splitlines()[-1] if out.strip() else "ok"
    return False, out.strip()[-200:]


def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Always-on host-side monitor daemon: writes pg_growth.csv, mongo_growth.csv, and resource_usage.csv. "
            "Designed to keep monitoring running even when docker compose is restarted."
        )
    )
    ap.add_argument("--interval-sec", type=int, default=10)
    ap.add_argument("--out-dir", default="/home/primary/data/monitor")
    ap.add_argument("--pg-container", default="postgres_live")
    ap.add_argument("--mongo-container", default="mongodb_live")
    ap.add_argument(
        "--mongo-uri",
        default="mongodb://mongodb:password@127.0.0.1:27017/test?authSource=admin",
        help="Mongo URI used from inside the mongodb container (auth enabled in live stack).",
    )
    ap.add_argument("--unstructured-dir", default="/home/primary/data/unstructured")
    ap.add_argument("--disk-path", default="/home/primary/data")
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()

    out_dir = os.path.abspath(args.out_dir)
    pg_csv = os.path.join(out_dir, "pg_growth.csv")
    mongo_csv = os.path.join(out_dir, "mongo_growth.csv")
    unstructured_csv = os.path.join(out_dir, "unstructured_totals.csv")
    res_csv = os.path.join(out_dir, "resource_usage.csv")

    ensure_csv_header(
        pg_csv,
        [
            "timestamp",
            "db",
            "action",
            "customers_added",
            "products_added",
            "orders_added",
            "customers_total",
            "products_total",
            "orders_total",
            "latency_ms",
        ],
    )
    ensure_csv_header(
        mongo_csv,
        [
            "timestamp",
            "db",
            "action",
            "products_added",
            "users_added",
            "orders_added",
            "products_total",
            "users_total",
            "orders_total",
            "latency_ms",
        ],
    )
    ensure_csv_header(
        unstructured_csv,
        [
            "timestamp",
            "db",
            "action",
            "files_added",
            "bytes_added",
            "files_total",
            "bytes_total",
            "latency_ms",
        ],
    )

    # resource_monitor already ensures its own header

    prev_pg: Totals | None = None
    prev_mongo: Totals | None = None
    prev_unstructured: tuple[int, int] | None = None

    _log("monitor", info_tag(), f"out_dir={out_dir} interval={int(args.interval_sec)}s")

    while True:
        ts = now_iso_jakarta()

        pg_totals, pg_err, pg_lat = read_pg_totals(container=args.pg_container)
        if pg_totals is not None:
            d = _delta(pg_totals, prev_pg)
            append_csv_row(pg_csv, [ts, "pgsql", "totals", d.a, d.b, d.c, pg_totals.a, pg_totals.b, pg_totals.c, f"{pg_lat:.1f}"])
            prev_pg = pg_totals
            _log("pg", good_tag(), f"totals={pg_totals.a},{pg_totals.b},{pg_totals.c} +{d.a},{d.b},{d.c} latency={pg_lat:.1f}ms")
        else:
            # Keep last-known totals (if any) so charts remain continuous during brief outages.
            if prev_pg is not None:
                append_csv_row(pg_csv, [ts, "pgsql", "down", 0, 0, 0, prev_pg.a, prev_pg.b, prev_pg.c, f"{pg_lat:.1f}"])
            else:
                append_csv_row(pg_csv, [ts, "pgsql", "down", 0, 0, 0, "", "", "", f"{pg_lat:.1f}"])
            _log("pg", bad_tag(), f"read failed: {pg_err or 'unknown'} latency={pg_lat:.1f}ms")

        mongo_totals, mongo_err, mongo_lat = read_mongo_totals(container=args.mongo_container, uri=args.mongo_uri)
        if mongo_totals is not None:
            d = _delta(mongo_totals, prev_mongo)
            append_csv_row(mongo_csv, [ts, "mongo", "totals", d.a, d.b, d.c, mongo_totals.a, mongo_totals.b, mongo_totals.c, f"{mongo_lat:.1f}"])
            prev_mongo = mongo_totals
            _log("mongo", good_tag(), f"totals={mongo_totals.a},{mongo_totals.b},{mongo_totals.c} +{d.a},{d.b},{d.c} latency={mongo_lat:.1f}ms")
        else:
            if prev_mongo is not None:
                append_csv_row(mongo_csv, [ts, "mongo", "down", 0, 0, 0, prev_mongo.a, prev_mongo.b, prev_mongo.c, f"{mongo_lat:.1f}"])
            else:
                append_csv_row(mongo_csv, [ts, "mongo", "down", 0, 0, 0, "", "", "", f"{mongo_lat:.1f}"])
            _log("mongo", bad_tag(), f"read failed: {mongo_err or 'unknown'} latency={mongo_lat:.1f}ms")

        u_files, u_bytes, u_lat = read_unstructured_totals(dir_path=args.unstructured_dir)
        if prev_unstructured is not None:
            dfiles = u_files - prev_unstructured[0]
            dbytes = u_bytes - prev_unstructured[1]
        else:
            dfiles = 0
            dbytes = 0

        append_csv_row(unstructured_csv, [ts, "unstructured", "totals", dfiles, dbytes, u_files, u_bytes, f"{u_lat:.1f}"])
        prev_unstructured = (u_files, u_bytes)
        _log("unstr", good_tag(), f"totals={u_files},{u_bytes} +{dfiles},{dbytes} latency={u_lat:.1f}ms")

        ok, res_msg = write_resource_row(out_csv=res_csv, disk_path=args.disk_path)
        _log("resource", good_tag() if ok else bad_tag(), res_msg)

        if args.once:
            return 0

        time.sleep(max(1, int(args.interval_sec)))


if __name__ == "__main__":
    raise SystemExit(main())
