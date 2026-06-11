import csv
import os
from datetime import datetime, timezone, timedelta

JAKARTA_TZ = timezone(timedelta(hours=7))

class BackupTelemetry:
    def __init__(self, out_csv: str = "/home/primary/data/monitor/backup_telemetry.csv"):
        self.out_csv = out_csv
        self.timings = {}
        self._ensure_header()
        self.cycle_id = None
        self.chain_version = None

    def _ensure_header(self):
        os.makedirs(os.path.dirname(self.out_csv), exist_ok=True)
        if not os.path.exists(self.out_csv):
            with open(self.out_csv, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "timestamp",
                    "cycle_id",
                    "chain_version",
                    "pg_basebackup_s",
                    "mongo_basebackup_s",
                    "pg_wal_s",
                    "mongo_oplog_extract_s",
                    "mongo_oplog_compress_s",
                    "unstructured_pfc_s",
                    "verify_s",
                "retention_s",
                "transfer_s",
                "total_cycle_s",
                "cycle_stored_size_bytes",
                "cycle_raw_size_bytes",
                "wal_counts",
                "pfc_counts"
            ])

    def start_cycle(self, cycle_id: str, chain_version: str):
        self.cycle_id = cycle_id
        self.chain_version = chain_version
        self.timings = {
            "pg_basebackup_s": 0.0,
            "mongo_basebackup_s": 0.0,
            "pg_wal_s": 0.0,
            "mongo_oplog_extract_s": 0.0,
            "mongo_oplog_compress_s": 0.0,
            "unstructured_pfc_s": 0.0,
            "verify_s": 0.0,
            "retention_s": 0.0,
            "transfer_s": 0.0,
            "total_cycle_s": 0.0
        }
        self.metrics = {
            "cycle_stored_size_bytes": 0,
            "cycle_raw_size_bytes": 0,
            "wal_counts": 0,
            "pfc_counts": 0
        }

    def record(self, key: str, elapsed_s: float):
        if key in self.timings:
            self.timings[key] = elapsed_s

    def record_metric(self, key: str, value: int):
        if key in self.metrics:
            self.metrics[key] = value

    def finalize(self, total_s: float):
        self.timings["total_cycle_s"] = total_s
        ts = datetime.now(JAKARTA_TZ).replace(microsecond=0).isoformat()
        row = [
            ts,
            self.cycle_id,
            self.chain_version,
            f"{self.timings['pg_basebackup_s']:.3f}",
            f"{self.timings['mongo_basebackup_s']:.3f}",
            f"{self.timings['pg_wal_s']:.3f}",
            f"{self.timings['mongo_oplog_extract_s']:.3f}",
            f"{self.timings['mongo_oplog_compress_s']:.3f}",
            f"{self.timings['unstructured_pfc_s']:.3f}",
            f"{self.timings['verify_s']:.3f}",
            f"{self.timings['retention_s']:.3f}",
            f"{self.timings['transfer_s']:.3f}",
            f"{self.timings['total_cycle_s']:.3f}",
            self.metrics["cycle_stored_size_bytes"],
            self.metrics["cycle_raw_size_bytes"],
            self.metrics["wal_counts"],
            self.metrics["pfc_counts"]
        ]
        with open(self.out_csv, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(row)
