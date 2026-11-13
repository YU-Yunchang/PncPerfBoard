"""Local HTTP application for PerfBoard."""
from __future__ import annotations

import argparse
import json
import mimetypes
import sqlite3
import subprocess
import shutil
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .perfboard_jobs import ImportQueue, find_records, output_for
from . import perfboard_voice as voice
from .perfboard_storage import (
    ACTIVE_STATUSES, connect, open_db, create_manual_event, create_run, delete_or_clear_event,
    delete_run_row, get_file_events, get_run, get_run_by_path, get_run_files, get_series,
    get_timeline, get_trajectory, init_db, list_runs, reset_for_retry, update_event,
    update_run_metadata,
)

STATIC_DIR = Path(__file__).with_name("perfboard_static")


def enriched_run(conn, run_id):
    run = get_run(conn, run_id)
    if not run:
        return None
    run["files"] = get_run_files(conn, run_id)
    return run


class Handler(BaseHTTPRequestHandler):
    server_version = "PerfBoard/1.0"

    @property
    def db_path(self):
        return self.server.db_path

    @property
    def imports(self):
        return self.server.imports

    def log_message(self, fmt, *args):
        print("[perfboard] " + fmt % args)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        try:
            parts = path.strip('/').split('/')
            if path == "/api/capabilities":
                return self.json({"local_transcription": voice.transcription_capability()})
            if len(parts) == 4 and parts[:2] == ['api', 'recordings']:
                rid, action = parts[2:]
                with open_db(self.db_path) as conn:
                    rec = voice.recording(conn, rid)
                if action == 'status':
                    return self.json(rec)
                if action == 'transcript':
                    return self.serve_media(Path(rec['directory']) / 'transcript.json', 'application/json', download=True)
                if action == 'audio':
                    return self.serve_media(Path(rec['directory']) / 'playback.m4a', 'audio/mp4')
            if path == "/api/runs":
                with open_db(self.db_path) as conn:
                    return self.json(list_runs(conn))
            if path == "/api/directories":
                return self.json(list_directories(parse_qs(parsed.query).get("path", [""])[0]))
            file_id, file_tail = parse_run_file_path(path)
            if file_id is not None and file_tail == "events":
                with open_db(self.db_path) as conn:
                    return self.json(get_file_events(conn, file_id))
            run_id, tail = parse_run_path(path)
            if run_id is not None:
                with open_db(self.db_path) as conn:
                    if tail == "":
                        value = enriched_run(conn, run_id)
                    elif tail == "trajectory":
                        value = get_trajectory(conn, run_id)
                    elif tail == "series":
                        signal = parse_qs(parsed.query).get("signal", ["closed_loop_active"])[0]
                        value = get_series(conn, run_id, signal)
                    elif tail == "timeline":
                        value = get_timeline(conn, run_id)
                    elif tail == "recordings":
                        value = voice.recordings(conn, run_id)
                    else:
                        return self.error(404, "not found")
                return self.json(value) if value is not None else self.error(404, "run not found")
            return self.static(parsed.path)
        except Exception as exc:
            return self.error(400, str(exc))

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/")
        try:
            parts = path.strip('/').split('/')
            if len(parts) == 4 and parts[:2] == ['api', 'recordings']:
                rid, action = parts[2:]
                if action == 'events':
                    with open_db(self.db_path) as conn:
                        result = voice.import_events(conn, rid, self.read_json())
                    return self.json(result, 201)
                if action == 'transcribe':
                    payload = self.read_json()
                    with open_db(self.db_path) as conn:
                        rec = voice.recording(conn, rid)
                        if payload.get('start_time'):
                            ns = voice.time_ns(payload['start_time'])
                            if ns != rec['start_time_ns']:
                                if rec['status'] in ('queued', 'processing'):
                                    raise ValueError('Recording is already processing')
                                if conn.execute('select 1 from voice_evidence where recording_id=?', (rid,)).fetchone():
                                    raise ValueError('Start time cannot change after events are imported')
                                voice.update_recording(conn, rid, start_time_ns=ns, time_source='manual')
                    self.server.voices.enqueue(rid)
                    return self.json({'status': 'queued'}, 202)
            if len(parts) == 4 and parts[:2] == ['api', 'events'] and parts[3] == 'link':
                payload = self.read_json()
                with open_db(self.db_path) as conn:
                    return self.json(voice.set_link(conn, int(parts[2]), payload.get('target_id'), payload.get('mode', 'manual')))
            run_id, tail = parse_run_path(path)
            if run_id is not None and tail == 'recordings':
                from urllib.parse import unquote
                with open_db(self.db_path) as conn:
                    rec = voice.register_recording(conn, self.db_path, run_id,
                        unquote(self.headers.get('X-Filename', 'audio.m4a')), self.rfile,
                        int(self.headers.get('Content-Length', '0')))
                return self.json(rec, 201)
            if path == "/api/runs/import":
                payload = self.read_json()
                root = Path(str(payload.get("data_path", ""))).expanduser().resolve()
                if not root.is_dir():
                    raise ValueError(f"data directory does not exist: {root}")
                name = str(payload.get("name", "")).strip() or root.name
                version = str(payload.get("version_tag", "")).strip()
                if not version:
                    raise ValueError("version tag is required")
                records = find_records(root)
                if not records:
                    raise ValueError("no record files found")
                pairs = [(source, output_for(source)) for source in records]
                with open_db(self.db_path) as conn:
                    if get_run_by_path(conn, str(root)):
                        raise ValueError("this data directory has already been imported")
                    run_id = create_run(conn, name, version, str(root), pairs)
                self.imports.enqueue(run_id)
                return self.json({"run_id": run_id, "status": "queued"}, 202)
            run_id, tail = parse_run_path(path)
            if run_id is not None and tail == "retry":
                with open_db(self.db_path) as conn:
                    reset_for_retry(conn, run_id)
                self.imports.enqueue(run_id)
                return self.json({"run_id": run_id, "status": "queued"}, 202)
            file_id, file_tail = parse_run_file_path(path)
            if file_id is not None and file_tail == "open":
                return self.open_mcap(file_id)
            if file_id is not None and file_tail == "events":
                payload = self.read_json()
                with open_db(self.db_path) as conn:
                    event = create_manual_event(
                        conn, file_id, int(payload.get("timestamp_ns")),
                        str(payload.get("reason") or "manual_note"), str(payload.get("description") or ""),
                    )
                return self.json(event, 201)
            return self.error(404, "not found")
        except sqlite3.IntegrityError:
            return self.error(409, "this data directory has already been imported")
        except Exception as exc:
            return self.error(400, str(exc))

    def do_PATCH(self):
        path = urlparse(self.path).path.rstrip("/")
        try:
            payload = self.read_json()
            event_id = parse_event_path(path)
            if event_id is not None:
                timestamp = payload.get("timestamp_ns")
                with open_db(self.db_path) as conn:
                    value = update_event(
                        conn, event_id, timestamp_ns=int(timestamp) if timestamp is not None else None,
                        reason=payload.get("reason"), description=payload.get("description"),
                        ignored=payload.get("ignored"), ignore_reason=payload.get("ignore_reason"),
                    )
                return self.json(value)
            run_id, tail = parse_run_path(path)
            if run_id is None or tail:
                return self.error(404, "not found")
            name, version = str(payload.get("name", "")).strip(), str(payload.get("version_tag", "")).strip()
            if not name or not version:
                raise ValueError("name and version tag are required")
            with open_db(self.db_path) as conn:
                update_run_metadata(conn, run_id, name, version)
            return self.json({"id": run_id, "name": name, "version_tag": version})
        except Exception as exc:
            return self.error(400, str(exc))

    def do_DELETE(self):
        path = urlparse(self.path).path.rstrip("/")
        try:
            event_id = parse_event_path(path)
            if event_id is not None:
                with open_db(self.db_path) as conn:
                    return self.json(delete_or_clear_event(conn, event_id))
            run_id, tail = parse_run_path(path)
            if run_id is None or tail:
                return self.error(404, "not found")
            with open_db(self.db_path) as conn:
                run = get_run(conn, run_id)
                if not run:
                    raise ValueError("run not found")
                if run["status"] in ACTIVE_STATUSES:
                    raise ValueError("cannot delete an active run")
                root = Path(run["data_path"]).resolve()
                if any(r['status'] in ('queued', 'processing') for r in voice.recordings(conn, run_id)):
                    raise ValueError('Cannot delete a run while audio is processing')
                files = get_run_files(conn, run_id)
                targets = []
                for item in files:
                    output = Path(item["output_path"]).resolve()
                    if output.suffix != ".mcap" or root not in output.parents:
                        raise ValueError(f"unsafe output path: {output}")
                    targets.extend((output, output.with_suffix(output.suffix + ".report.json")))
                for target in targets:
                    if target.exists():
                        target.unlink()
                recording_rows = voice.recordings(conn, run_id)
                delete_run_row(conn, run_id)
                for item in recording_rows:
                    shutil.rmtree(item['directory'], ignore_errors=True)
            return self.json({"id": run_id, "deleted": True})
        except Exception as exc:
            return self.error(400, str(exc))

    def open_mcap(self, file_id):
        with open_db(self.db_path) as conn:
            row = conn.execute(
                "select f.*,r.data_path from run_files f join runs r on r.id=f.run_id where f.id=?",
                (file_id,),
            ).fetchone()
        if row is None or row["status"] not in ("converted", "reused"):
            return self.error(400, "MCAP is not ready")
        path, root = Path(row["output_path"]).resolve(), Path(row["data_path"]).resolve()
        if path.suffix != ".mcap" or root not in path.parents or not path.is_file():
            return self.error(400, "MCAP file is missing")
        if sys.platform == "win32":
            os.startfile(str(path))
            return self.json({"opened": True, "path": str(path)}, 202)
        command = "open" if sys.platform == "darwin" else "xdg-open"
        if not shutil.which(command):
            return self.error(400, f"Install Foxglove and open this file manually: {path}")
        result = subprocess.run([command, str(path)], capture_output=True, text=True, timeout=10)
        if result.returncode:
            detail = result.stderr.strip() or "Foxglove could not open the MCAP file"
            return self.error(500, detail)
        return self.json({"opened": True, "path": str(path)}, 202)

    def read_json(self):
        size = int(self.headers.get("content-length", "0"))
        if size > 20 * 1024 * 1024:
            raise ValueError('JSON payload exceeds 20 MiB')
        return json.loads(self.rfile.read(size).decode()) if size else {}

    def json(self, value, status=200):
        body = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status); self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def error(self, status, message):
        self.json({"error": message}, status)

    def serve_media(self, path, content_type, download=False):
        if not path.is_file():
            return self.error(404, 'Media is not ready')
        size = path.stat().st_size
        start, end, status = 0, size - 1, 200
        request = self.headers.get('Range')
        if request:
            import re
            match = re.fullmatch(r'bytes=(\d*)-(\d*)', request)
            if not match or not any(match.groups()):
                return self.error(416, 'Invalid byte range')
            a, b = match.groups()
            start = int(a) if a else max(0, size - int(b))
            end = min(size - 1, int(b)) if a and b else size - 1
            if start > end or start >= size:
                self.send_response(416); self.send_header('Content-Range', f'bytes */{size}'); self.end_headers(); return
            status = 206
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Content-Length', str(end - start + 1))
        if status == 206:
            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        if download:
            self.send_header('Content-Disposition', 'attachment; filename="transcript.json"')
        self.end_headers()
        with path.open('rb') as stream:
            stream.seek(start)
            remaining = end - start + 1
            while remaining:
                chunk = stream.read(min(65536, remaining))
                if not chunk: break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return  # Browsers routinely abort the previous range when seeking.
                remaining -= len(chunk)

    def static(self, request_path):
        name = "index.html" if request_path in ("", "/") else request_path.lstrip("/")
        target = (STATIC_DIR / name).resolve()
        if STATIC_DIR.resolve() not in target.parents or not target.is_file():
            return self.error(404, "not found")
        data = target.read_bytes(); self.send_response(200)
        self.send_header("content-type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
        self.send_header("content-length", str(len(data))); self.end_headers(); self.wfile.write(data)


def parse_run_path(path):
    parts = path.strip("/").split("/")
    if len(parts) >= 3 and parts[:2] == ["api", "runs"] and parts[2].isdigit():
        return int(parts[2]), "/".join(parts[3:])
    return None, None


def parse_file_open_path(path):
    file_id, tail = parse_run_file_path(path)
    return file_id if tail == "open" else None


def parse_run_file_path(path):
    parts = path.strip("/").split("/")
    if len(parts) == 4 and parts[:2] == ["api", "run-files"] and parts[2].isdigit():
        return int(parts[2]), parts[3]
    return None, None


def parse_event_path(path):
    parts = path.strip("/").split("/")
    return int(parts[2]) if len(parts) == 3 and parts[:2] == ["api", "events"] and parts[2].isdigit() else None


def list_directories(value):
    path = Path(value).expanduser().resolve() if value else (Path.home() / "Documents")
    if not path.exists():
        raise ValueError("directory does not exist")
    if not path.is_dir(): path = path.parent
    entries = []
    for child in path.iterdir():
        try:
            if child.is_dir(): entries.append({"name": child.name, "path": str(child.resolve())})
        except OSError: pass
    return {"path": str(path), "parent": str(path.parent) if path.parent != path else None,
            "entries": sorted(entries, key=lambda item: item["name"].lower()),
            "shortcuts": [{"label": "Home", "path": str(Path.home())},
                          {"label": "Documents", "path": str(Path.home() / "Documents")} ]}


def main(host="127.0.0.1", port=8766, db="data/perfboard.sqlite3"):
    db_path = Path(db).resolve()
    with open_db(db_path) as conn: init_db(conn)
    server = ThreadingHTTPServer((host, port), Handler)
    server.db_path = db_path; server.imports = ImportQueue(db_path); server.voices = voice.VoiceQueue(db_path)
    print(f"PerfBoard running at http://{host}:{port}", flush=True)
    server.serve_forever()


def cli_main():
    parser = argparse.ArgumentParser(description="Run the local PerfBoard server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--db", default="data/perfboard.sqlite3")
    args = parser.parse_args(); main(args.host, args.port, args.db)
