"""Offline recordings, portable voice contracts, and takeover matching."""
from __future__ import annotations

import hashlib
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
PROJECT = Path(__file__).resolve().parent.parent


def time_ns(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("Recording start time must include a timezone")
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = dt.astimezone(timezone.utc) - epoch
    return (delta.days * 86400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1000


def iso_time(ns):
    return datetime.fromtimestamp(int(ns) / 1e9, timezone.utc).isoformat().replace("+00:00", "Z")


def init_voice(conn):
    # SQLite CHECK constraints require a table rebuild. Preserve every existing column/ID.
    sql = conn.execute("select sql from sqlite_master where name='events'").fetchone()[0]
    if "'voice'" not in sql:
        replacement = sql.replace("CREATE TABLE events", "CREATE TABLE events_voice", 1).replace(
            "create table events", "create table events_voice", 1).replace(
            "('auto','manual')", "('auto','manual','voice')")
        conn.commit()
        conn.execute("pragma foreign_keys=off")
        try:
            conn.execute("begin")
            conn.execute(replacement)
            columns = ",".join(row[1] for row in conn.execute("pragma table_info(events)"))
            conn.execute(f"insert into events_voice({columns}) select {columns} from events")
            conn.execute("drop table events")
            conn.execute("alter table events_voice rename to events")
            conn.execute("create index idx_events_run_time on events(run_id,timestamp_ns)")
            conn.execute("create index idx_events_file_time on events(run_file_id,timestamp_ns)")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.execute("pragma foreign_keys=on")
    conn.executescript("""
        create table if not exists recordings (
            id text primary key, run_id integer not null references runs(id) on delete cascade,
            filename text not null, sha256 text not null, directory text not null,
            status text not null, stage text, error text, duration_ms integer,
            raw_start_time text, start_time_ns integer, time_source text,
            processed_ms integer not null default 0,
            created_at text not null default current_timestamp
        );
        create table if not exists voice_evidence (
            event_id integer primary key references events(id) on delete cascade,
            recording_id text not null references recordings(id) on delete cascade,
            external_id text not null, category text not null, title text not null,
            takeover text not null, start_ms integer not null, end_ms integer not null,
            segment_ids text not null,
            linked_event_id integer references events(id) on delete set null,
            link_mode text not null default 'auto', match_status text not null default 'unmatched',
            unique(recording_id,external_id)
        );
    """)
    conn.execute("update recordings set status='interrupted',stage='Interrupted',error='Server stopped during transcription' where status in ('queued','processing')")
    conn.commit()


def tool_paths():
    python = Path(os.environ.get("PERFBOARD_WHISPER_PYTHON", PROJECT / ".venv-transcribe/bin/python"))
    ffmpeg = os.environ.get("PERFBOARD_FFMPEG") or shutil.which("ffmpeg")
    if not ffmpeg:
        bundled = PROJECT / ".venv-transcribe/lib"
        ffmpeg = next(iter(bundled.glob("python*/site-packages/imageio_ffmpeg/binaries/ffmpeg-*")), None)
    if not python.is_file() or not ffmpeg or not Path(ffmpeg).is_file():
        raise ValueError("Local Whisper environment or FFmpeg is missing. Configure PERFBOARD_WHISPER_PYTHON and PERFBOARD_FFMPEG.")
    return str(python), str(ffmpeg)


def transcription_capability():
    try:
        tool_paths()
    except ValueError:
        return {"available": False, "message": "Local transcription is optional and is not configured. Run import, MCAP conversion and manual events are available. See docs/voice-events.md for setup."}
    return {"available": True, "message": "Local Whisper environment configured. Transcription requires a cached model."}


def recording(conn, rid):
    row = conn.execute("select * from recordings where id=?", (rid,)).fetchone()
    if not row:
        raise ValueError("Recording not found")
    return dict(row)


def recordings(conn, run_id):
    return [dict(r) for r in conn.execute("""select r.*,(select count(*) from voice_evidence where recording_id=r.id) as event_count
                 from recordings r where run_id=? order by created_at,id""", (run_id,))]


def update_recording(conn, rid, **values):
    conn.execute("update recordings set " + ",".join(f"{k}=?" for k in values) + " where id=?", (*values.values(), rid))
    conn.commit()


def probe(ffmpeg, path):
    result = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path)], capture_output=True, text=True, timeout=30)
    import re
    duration = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", result.stderr)
    if not duration:
        raise ValueError("Audio could not be decoded")
    h, m, s = duration.groups()
    ms = round((int(h) * 3600 + int(m) * 60 + float(s)) * 1000)
    creation = re.search(r"creation_time\s*:\s*(\S+)", result.stderr)
    return ms, creation.group(1) if creation else None


def register_recording(conn, db_path, run_id, filename, stream, size, start=None):
    if not conn.execute("select 1 from runs where id=?", (run_id,)).fetchone():
        raise ValueError("Run not found")
    suffix = Path(filename).suffix.lower()
    if suffix not in (".m4a", ".mp3", ".wav"):
        raise ValueError("Supported audio formats: M4A, MP3, WAV")
    if not 0 < size <= 512 * 1024 * 1024:
        raise ValueError("Audio must be between 1 byte and 512 MiB")
    _, ffmpeg = tool_paths()
    rid = str(uuid.uuid4())
    directory = Path(db_path).resolve().parent / "voice" / rid
    directory.mkdir(parents=True)
    try:
        digest = hashlib.sha256()
        with (directory / ("original" + suffix)).open("wb") as out:
            remaining = size
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ValueError("Incomplete audio upload")
                out.write(chunk); digest.update(chunk); remaining -= len(chunk)
        duration, raw = probe(ffmpeg, directory / ("original" + suffix))
        effective = start or raw
        ns = time_ns(effective) if effective else None
        conn.execute("""insert into recordings(id,run_id,filename,sha256,directory,status,stage,duration_ms,
                     raw_start_time,start_time_ns,time_source) values(?,?,?,?,?,'uploaded','Uploaded',?,?,?,?)""",
                     (rid, run_id, Path(filename).name, digest.hexdigest(), str(directory), duration,
                      raw, ns, "manual" if start else "metadata" if raw else "missing"))
        conn.commit()
        return recording(conn, rid)
    except Exception:
        shutil.rmtree(directory)
        raise


def normalize_transcript(rec, raw):
    if rec["start_time_ns"] is None:
        raise ValueError("Recording start time is required")
    segments = []
    for index, item in enumerate(raw["segments"]):
        start = max(0, round(float(item["start"]) * 1000))
        end = min(rec["duration_ms"], round(float(item["end"]) * 1000))
        if end <= start:
            continue
        text = str(item["text"]).strip()
        segments.append({"id": f"s{index:04d}", "start_ms": start, "end_ms": end, "text": text,
                         "absolute_start": iso_time(rec["start_time_ns"] + start * 1_000_000),
                         "suspect": item.get("compression_ratio", 0) > 2.4 or item.get("avg_logprob", 0) < -1,
                         "avg_logprob": item.get("avg_logprob"), "no_speech_prob": item.get("no_speech_prob")})
    return {"schema_version": 1, "kind": "perfboard_transcript", "recording_id": rec["id"],
            "sha256": rec["sha256"], "filename": rec["filename"], "duration_ms": rec["duration_ms"],
            "raw_start_time": rec["raw_start_time"], "start_time_ns": str(rec["start_time_ns"]),
            "start_time": iso_time(rec["start_time_ns"]), "time_source": rec["time_source"],
            "model": "mlx-community/whisper-large-v3-turbo", "segments": segments}


def validate_events(payload, transcript):
    from .perfboard_storage import EVENT_REASONS
    if payload.get("schema_version") != 1 or payload.get("kind") != "perfboard_voice_events":
        raise ValueError("Unsupported voice event contract")
    for key in ("recording_id", "sha256"):
        if payload.get(key) != transcript[key]:
            raise ValueError(f"Recording {key} mismatch")
    segments = {s["id"]: s for s in transcript["segments"]}
    result, ids = [], set()
    if not isinstance(payload.get("events"), list):
        raise ValueError("Events must be an array")
    for item in payload["events"]:
        external = item.get("id")
        if not isinstance(external, str) or not external.strip() or external in ids:
            raise ValueError("Event IDs must be unique nonempty strings")
        ids.add(external)
        category, takeover = item.get("category"), item.get("takeover", "none")
        if category not in ("issue", "check", "positive") or takeover not in ("none", "brake", "steering", "unspecified"):
            raise ValueError("Invalid event category or takeover type")
        refs = item.get("segment_ids")
        if not isinstance(refs, list) or not refs or any(ref not in segments for ref in refs):
            raise ValueError("Event requires valid evidence segment IDs")
        start, end = min(segments[r]["start_ms"] for r in refs), max(segments[r]["end_ms"] for r in refs)
        ns = int(transcript["start_time_ns"]) + start * 1_000_000
        if not isinstance(item.get("title"), str) or not item["title"].strip() or not isinstance(item.get("description"), str) or not item["description"].strip():
            raise ValueError("Event title and description are required")
        reason = item.get("reason", "other")
        if reason not in EVENT_REASONS:
            raise ValueError("Invalid event reason")
        if "timestamp_ns" in item and (not isinstance(item["timestamp_ns"], str) or int(item["timestamp_ns"]) != ns):
            raise ValueError("Event timestamp must equal the first evidence segment time")
        result.append(dict(item, start_ms=start, end_ms=end, timestamp_ns=str(ns), reason=reason, takeover=takeover))
    return result


def match_voice(conn, run_id):
    exits = [dict(r) for r in conn.execute("select * from events where run_id=? and source='auto' and event_type='closed_loop_exit'", (run_id,))]
    voices = conn.execute("select v.*,e.timestamp_ns from voice_evidence v join events e on e.id=v.event_id where e.run_id=?", (run_id,)).fetchall()
    for voice in voices:
        if voice["link_mode"] != "auto":
            if voice["link_mode"] == "manual" and voice["linked_event_id"] is None:
                conn.execute("update voice_evidence set link_mode='auto',match_status='unmatched' where event_id=?", (voice["event_id"],))
            else:
                continue
        candidates = [e for e in exits if voice["timestamp_ns"] - 30_000_000_000 <= e["timestamp_ns"] <= voice["timestamp_ns"] + 5_000_000_000]
        window_candidates = candidates[:]
        if voice["takeover"] == "none":
            candidates = []
        elif voice["takeover"] in ("brake", "steering"):
            candidates = [e for e in candidates if e["detected_reason"] == voice["takeover"] + "_takeover"]
        candidates.sort(key=lambda e: (abs(e["timestamp_ns"] - voice["timestamp_ns"]), e["id"]))
        target, status = None, "unmatched"
        if voice['takeover'] == 'none':
            status = 'not_applicable'
        if not candidates and window_candidates and voice['takeover'] in ('brake', 'steering'):
            status = 'type_conflict'
        if candidates:
            if len(candidates) == 1 or abs(candidates[1]["timestamp_ns"] - voice["timestamp_ns"]) - abs(candidates[0]["timestamp_ns"] - voice["timestamp_ns"]) >= 5_000_000_000:
                target, status = candidates[0]["id"], "linked"
            else:
                status = "ambiguous"
        conn.execute("update voice_evidence set linked_event_id=?,match_status=? where event_id=?", (target, status, voice["event_id"]))


def import_events(conn, rid, payload):
    rec = recording(conn, rid)
    path = Path(rec["directory"]) / "transcript.json"
    if rec["status"] != "ready" or not path.is_file():
        raise ValueError("Transcript is not ready")
    items = validate_events(payload, json.loads(path.read_text()))
    from .perfboard_storage import get_run_files
    files = get_run_files(conn, rec["run_id"])
    added = skipped = 0
    with conn:
        for item in items:
            if conn.execute("select 1 from voice_evidence where recording_id=? and external_id=?", (rid, item["id"])).fetchone():
                skipped += 1; continue
            ns = int(item["timestamp_ns"])
            cursor = conn.execute("""insert into events(run_id,run_file_id,timestamp_ns,event_type,detected_reason,
                                 source,auto_description,diagnostic_json) values(?,?,?,'voice_report',?,'voice',?,'{}')""",
                                 (rec["run_id"], voice_file(files, ns), ns, item["reason"], item["description"]))
            conn.execute("""insert into voice_evidence(event_id,recording_id,external_id,category,title,takeover,start_ms,end_ms,segment_ids)
                         values(?,?,?,?,?,?,?,?,?)""", (cursor.lastrowid, rid, item["id"], item["category"], item["title"], item["takeover"],
                         item["start_ms"], item["end_ms"], json.dumps(item["segment_ids"])))
            added += 1
        match_voice(conn, rec["run_id"])
    return {"added": added, "skipped": skipped}


def voice_file(files, ns):
    return next((f["id"] for f in files if f["begin_time_ns"] is not None and f["end_time_ns"] is not None
                 and f["begin_time_ns"] <= ns <= f["end_time_ns"]), None)


def enrich_events(conn, events):
    by_id = {e["id"]: e for e in events}
    if not events:
        return events
    recs, transcripts = {}, {}
    for row in conn.execute("select * from voice_evidence"):
        event = by_id.get(row["event_id"])
        if not event:
            continue
        evidence = dict(row)
        rid = row["recording_id"]
        if rid not in recs:
            recs[rid] = recording(conn, rid)
            file = Path(recs[rid]["directory"]) / "transcript.json"
            transcripts[rid] = json.loads(file.read_text()) if file.is_file() else {"segments": []}
        rec = recs[rid]
        refs = json.loads(evidence.pop("segment_ids"))
        evidence["transcript"] = "\n".join(s["text"] for s in transcripts[rid]["segments"] if s["id"] in refs)
        evidence["absolute_start"] = iso_time(rec["start_time_ns"] + row["start_ms"] * 1_000_000)
        evidence["absolute_end"] = iso_time(rec["start_time_ns"] + row["end_ms"] * 1_000_000)
        evidence["audio_url"] = f"/api/recordings/{rid}/audio"
        evidence["duration_ms"] = rec["duration_ms"]
        target = by_id.get(row["linked_event_id"])
        if not (target and row["match_status"] == "linked" and row["link_mode"] != "disabled"
                and target["source"] == "auto" and target["event_type"] == "closed_loop_exit"
                and target["run_id"] == event["run_id"]):
            target = None
        # The nested evidence category (issue/check/positive) is the transcript
        # assessment/large heading. The top-level category supplies grey type metadata:
        # inherit its automatic exit's effective category, or remain a manual note.
        event["category"] = target["category"] if target else "manual_note"
        evidence["delta_s"] = (event["timestamp_ns"] - target["timestamp_ns"]) / 1e9 if target else None
        event["voice"] = evidence
        if target:
            target.setdefault("voice_reports", []).append(event)
        event["link_candidates"] = [{"id": e["id"], "timestamp_ns": str(e["timestamp_ns"]),
                                     "reason": e["reason"], "category": e["category"]}
                                    for e in events if e["source"] == "auto" and e["event_type"] == "closed_loop_exit"]
    return events


def set_link(conn, event_id, target=None, mode="manual"):
    voice = conn.execute("select v.*,e.run_id from voice_evidence v join events e on e.id=v.event_id where event_id=?", (event_id,)).fetchone()
    if not voice:
        raise ValueError("Voice event not found")
    if mode not in ("manual", "auto", "disabled"):
        raise ValueError("Invalid link mode")
    if mode == 'manual' and target is None:
        raise ValueError('Select an automatic exit to link')
    if mode != 'manual':
        target = None
    if target is not None:
        row = conn.execute("select * from events where id=?", (target,)).fetchone()
        if not row or row["run_id"] != voice["run_id"] or row["source"] != "auto" or row["event_type"] != "closed_loop_exit":
            raise ValueError("Link target must be an automatic exit in the same run")
    conn.execute("update voice_evidence set linked_event_id=?,link_mode=?,match_status=? where event_id=?",
                 (target, mode, "linked" if target else "unmatched", event_id))
    if mode == "auto":
        match_voice(conn, voice["run_id"])
    conn.commit()
    return {"updated": True}


class VoiceQueue:
    def __init__(self, db_path):
        self.db_path = db_path
        self.pending = queue.Queue()
        self.lock = threading.Lock()
        self.active = set()
        threading.Thread(target=self.worker, name="perfboard-voice", daemon=True).start()

    def enqueue(self, rid):
        from .perfboard_storage import connect
        with self.lock, connect(self.db_path) as conn:
            rec = recording(conn, rid)
            if rid in self.active:
                raise ValueError("Recording is already processing")
            if rec["start_time_ns"] is None:
                raise ValueError("Recording start time is required")
            if conn.execute('select 1 from voice_evidence where recording_id=?', (rid,)).fetchone():
                raise ValueError('Transcript is locked after event import; upload a new recording to transcribe again')
            self.active.add(rid)
            update_recording(conn, rid, status="queued", stage="Queued", error=None, processed_ms=0)
            self.pending.put(rid)

    def worker(self):
        from .perfboard_storage import connect
        while True:
            rid = self.pending.get()
            try:
                self.process(rid)
            except Exception as exc:
                with connect(self.db_path) as conn:
                    update_recording(conn, rid, status="failed", stage="Failed", error=str(exc)[-2000:])
            finally:
                with self.lock:
                    self.active.discard(rid)
                self.pending.task_done()

    def process(self, rid):
        from .perfboard_storage import connect
        with connect(self.db_path) as conn:
            rec = recording(conn, rid)
            directory = Path(rec["directory"])
            python, ffmpeg = tool_paths()
            update_recording(conn, rid, status="processing", stage="Decoding")
            original = next(directory.glob("original.*"))
            subprocess.run([ffmpeg, "-y", "-i", str(original), "-vn", "-c:a", "aac", "-b:a", "96k", str(directory / "playback.m4a")],
                           check=True, capture_output=True, timeout=600)
            update_recording(conn, rid, stage="Transcribing")
            command = [python, str(Path(__file__).with_name("voice_worker.py")), str(original), str(directory), ffmpeg]
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            lines = []
            for line in process.stdout:
                lines.append(line[-1000:]); lines = lines[-20:]
                if line.startswith("PROGRESS "):
                    update_recording(conn, rid, processed_ms=int(line.split()[1]))
            if process.wait():
                raise ValueError("Whisper failed: " + "".join(lines))
            raw = json.loads((directory / "raw.json").read_text())
            (directory / "transcript.json").write_text(json.dumps(normalize_transcript(rec, raw), ensure_ascii=False, indent=2))
            update_recording(conn, rid, status="ready", stage="Ready for event extraction", processed_ms=rec["duration_ms"])
