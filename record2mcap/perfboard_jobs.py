"""Single-worker import queue for PerfBoard."""
from __future__ import annotations

import json
import queue
import threading
from pathlib import Path

from .converter import convert, validate
from .cyber import RecordReader
from .perfboard_analysis import RunAnalyzer, scan_record
from .perfboard_storage import connect, get_run, get_run_files, save_analysis, set_run_state, update_file


def find_records(root: Path):
    return sorted(p for p in root.rglob("*") if p.is_file() and
                  (p.name.endswith(".record") or ".record." in p.name))


def output_for(source: Path):
    base = source.parent.parent if source.parent.name == "gaea" else source.parent
    name = source.name
    if name.endswith(".record"):
        name = name[:-7]
    return base / "mcap" / f"{name}.mcap"


class ImportQueue:
    def __init__(self, db_path):
        self.db_path = Path(db_path)
        self.pending = queue.Queue()
        self.thread = threading.Thread(target=self._worker, name="perfboard-import", daemon=True)
        self.thread.start()

    def enqueue(self, run_id):
        self.pending.put(int(run_id))

    def _worker(self):
        while True:
            run_id = self.pending.get()
            try:
                self._import(run_id)
            except Exception as exc:
                with connect(self.db_path) as conn:
                    set_run_state(conn, run_id, "failed", error_summary=str(exc), current_file=None)
            finally:
                self.pending.task_done()

    def _import(self, run_id):
        conn = connect(self.db_path)
        run = get_run(conn, run_id)
        if not run:
            conn.close(); return
        files = get_run_files(conn, run_id)
        set_run_state(conn, run_id, "importing", current_file=None)
        analyzer = RunAnalyzer()
        failures = []
        completed = 0
        for item in files:
            source, output = Path(item["source_path"]), Path(item["output_path"])
            set_run_state(conn, run_id, "importing", completed_files=completed, current_file=source.name)
            checkpoint = analyzer.checkpoint()
            try:
                header = RecordReader(source).header
                analyzer.observe_range(header.begin_time, header.end_time)
                update_file(conn, item["id"], item["status"], begin_time_ns=header.begin_time,
                            end_time_ns=header.end_time, error=item["error"])
                if item["status"] in ("converted", "reused") and output.exists():
                    validate(output, source=source)
                    scan_record(source, analyzer)
                    status = item["status"]
                    report_path = output.with_suffix(output.suffix + ".report.json")
                    report = json.loads(report_path.read_text()) if report_path.exists() else {}
                elif output.exists():
                    validate(output, source=source)
                    scan_record(source, analyzer)
                    status = "reused"
                    report_path = output.with_suffix(output.suffix + ".report.json")
                    report = json.loads(report_path.read_text()) if report_path.exists() else {}
                else:
                    update_file(conn, item["id"], "converting", error=None)
                    report = convert(source, output, foxglove=True)
                    scan_record(source, analyzer)
                    status = "converted"
                update_file(
                    conn, item["id"], status, output_size=output.stat().st_size,
                    original_messages=report.get("original_messages"),
                    derived_messages=sum(report.get("derived_channels", {}).values()),
                    begin_time_ns=header.begin_time, end_time_ns=header.end_time, error=None,
                )
            except Exception as exc:
                analyzer.restore(checkpoint)
                failures.append(f"{source.name}: {exc}")
                update_file(conn, item["id"], "failed", error=str(exc))
            completed += 1
        analyzer.finalize()
        save_analysis(conn, run_id, analyzer)
        status = "failed" if failures else "ready"
        set_run_state(
            conn, run_id, status, completed_files=completed, current_file=None,
            error_summary="\n".join(failures) or None,
        )
        conn.execute("update runs set completed_at=current_timestamp where id=?", (run_id,))
        conn.commit(); conn.close()
