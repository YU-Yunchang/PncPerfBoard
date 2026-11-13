"""SQLite persistence for the local PerfBoard application."""
from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path


ACTIVE_STATUSES = {"queued", "importing"}
ANALYSIS_VERSION = 2
CLOSED_STATES = {"active", "longitudinal_override"}
EVENT_REASONS = {
    "brake_takeover", "steering_takeover", "eps_abort", "closed_loop_disabled",
    "feedback_lost", "other", "longitudinal_override", "longitudinal_override_started", "longitudinal_override_ended",
    "manual_note",
}


def connect(path: str | Path) -> sqlite3.Connection:
    db = Path(path)
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma foreign_keys = on")
    conn.execute("pragma journal_mode = wal")
    return conn


def _columns(conn, table):
    return {row["name"] for row in conn.execute(f"pragma table_info({table})")}


def _add_column(conn, table, definition):
    name = definition.split()[0]
    if name not in _columns(conn, table):
        conn.execute(f"alter table {table} add column {definition}")


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        create table if not exists runs (
            id integer primary key autoincrement,
            name text not null,
            version_tag text not null,
            data_path text not null unique,
            status text not null default 'queued',
            total_files integer not null default 0,
            completed_files integer not null default 0,
            current_file text,
            error_summary text,
            warning_summary text,
            start_time_ns integer,
            end_time_ns integer,
            distance_m real not null default 0,
            gps_point_count integer not null default 0,
            analysis_version integer not null default 2,
            created_at text not null default current_timestamp,
            completed_at text
        );
        create table if not exists run_files (
            id integer primary key autoincrement,
            run_id integer not null references runs(id) on delete cascade,
            source_path text not null,
            output_path text not null,
            status text not null default 'pending',
            source_size integer not null,
            output_size integer,
            original_messages integer,
            derived_messages integer,
            begin_time_ns integer,
            end_time_ns integer,
            error text,
            unique(run_id, source_path)
        );
        create table if not exists gps_points (
            run_id integer not null references runs(id) on delete cascade,
            timestamp_ns integer not null,
            lon real not null,
            lat real not null,
            height real,
            primary key(run_id, timestamp_ns)
        ) without rowid;
        create table if not exists signal_events (
            run_id integer not null references runs(id) on delete cascade,
            signal_key text not null,
            timestamp_ns integer not null,
            value real not null,
            primary key(run_id, signal_key, timestamp_ns)
        ) without rowid;
        create table if not exists state_segments (
            id integer primary key autoincrement,
            run_id integer not null references runs(id) on delete cascade,
            start_time_ns integer not null,
            end_time_ns integer not null,
            state text not null,
            reason text,
            evidence_json text,
            unique(run_id, start_time_ns)
        );
        create table if not exists events (
            id integer primary key autoincrement,
            run_id integer not null references runs(id) on delete cascade,
            run_file_id integer references run_files(id) on delete cascade,
            timestamp_ns integer not null,
            end_time_ns integer,
            event_type text not null,
            detected_reason text,
            effective_reason text,
            source text not null check(source in ('auto','manual')),
            auto_description text,
            user_description text,
            diagnostic_json text,
            created_at text not null default current_timestamp,
            updated_at text not null default current_timestamp
        );
        create index if not exists idx_run_files_run on run_files(run_id, id);
        create index if not exists idx_segments_run_time on state_segments(run_id, start_time_ns);
        create index if not exists idx_events_run_time on events(run_id, timestamp_ns);
        create index if not exists idx_events_file_time on events(run_file_id, timestamp_ns);
    """)
    _add_column(conn, "events", "ignored integer not null default 0")
    _add_column(conn, "events", "ignore_reason text")
    # Additive migrations preserve all existing runs as legacy analysis.
    _add_column(conn, "runs", "analysis_version integer not null default 1")
    _add_column(conn, "run_files", "begin_time_ns integer")
    _add_column(conn, "run_files", "end_time_ns integer")
    from .perfboard_voice import init_voice
    init_voice(conn)
    conn.execute("update runs set status='interrupted', error_summary='Server stopped during import' where status in ('queued','importing')")
    conn.execute("update run_files set status='pending' where status='converting'")
    conn.commit()


def row_dict(row):
    return dict(row) if row is not None else None


def list_runs(conn):
    return [dict(row) for row in conn.execute("select * from runs order by id desc")]


def get_run(conn, run_id: int):
    return row_dict(conn.execute("select * from runs where id=?", (run_id,)).fetchone())


def get_run_by_path(conn, data_path: str):
    return row_dict(conn.execute("select * from runs where data_path=?", (data_path,)).fetchone())


def get_run_files(conn, run_id: int):
    return [dict(row) for row in conn.execute("select * from run_files where run_id=? order by source_path", (run_id,))]


def create_run(conn, name: str, version_tag: str, data_path: str, files) -> int:
    cur = conn.execute(
        "insert into runs(name,version_tag,data_path,status,total_files,analysis_version) values(?,?,?,'queued',?,?)",
        (name, version_tag, data_path, len(files), ANALYSIS_VERSION),
    )
    run_id = int(cur.lastrowid)
    conn.executemany(
        "insert into run_files(run_id,source_path,output_path,source_size) values(?,?,?,?)",
        [(run_id, str(source), str(output), source.stat().st_size) for source, output in files],
    )
    conn.commit()
    return run_id


def update_run_metadata(conn, run_id: int, name: str, version_tag: str):
    cur = conn.execute("update runs set name=?,version_tag=? where id=?", (name, version_tag, run_id))
    if cur.rowcount != 1:
        raise ValueError("run not found")
    conn.commit()


def set_run_state(conn, run_id: int, status: str, **values):
    allowed = {"completed_files", "current_file", "error_summary", "warning_summary", "completed_at"}
    invalid = set(values) - allowed
    if invalid:
        raise ValueError(f"invalid run fields: {sorted(invalid)}")
    assignments = ["status=?"] + [f"{key}=?" for key in values]
    conn.execute(f"update runs set {','.join(assignments)} where id=?", (status, *values.values(), run_id))
    conn.commit()


def update_file(conn, file_id: int, status: str, **values):
    allowed = {"output_size", "original_messages", "derived_messages", "begin_time_ns", "end_time_ns", "error"}
    invalid = set(values) - allowed
    if invalid:
        raise ValueError(f"invalid file fields: {sorted(invalid)}")
    assignments = ["status=?"] + [f"{key}=?" for key in values]
    conn.execute(f"update run_files set {','.join(assignments)} where id=?", (status, *values.values(), file_id))
    conn.commit()


def _file_for_timestamp(files, timestamp_ns):
    for item in files:
        begin, end = item.get("begin_time_ns"), item.get("end_time_ns")
        if begin is not None and end is not None and begin <= timestamp_ns <= end:
            return item["id"]
    candidates = [item for item in files if item.get("begin_time_ns") is not None]
    return min(candidates, key=lambda item: abs(item["begin_time_ns"] - timestamp_ns))["id"] if candidates else None


def save_analysis(conn, run_id: int, analyzer) -> None:
    files = get_run_files(conn, run_id)
    previous = [dict(row) for row in conn.execute(
        "select * from events where run_id=? and source='auto' order by timestamp_ns", (run_id,)
    )]
    conn.execute("delete from gps_points where run_id=?", (run_id,))
    conn.execute("delete from signal_events where run_id=?", (run_id,))
    conn.execute("delete from state_segments where run_id=?", (run_id,))
    conn.executemany(
        "insert into gps_points(run_id,timestamp_ns,lon,lat,height) values(?,?,?,?,?)",
        [(run_id, *point) for point in analyzer.gps_points],
    )
    conn.executemany(
        "insert into signal_events(run_id,signal_key,timestamp_ns,value) values(?,?,?,?)",
        [(run_id, "closed_loop_active", timestamp, value) for timestamp, value in analyzer.closed_loop_events],
    )
    conn.executemany(
        "insert into state_segments(run_id,start_time_ns,end_time_ns,state,reason,evidence_json) values(?,?,?,?,?,?)",
        [(run_id, item["start_time_ns"], item["end_time_ns"], item["state"], item.get("reason"),
          json.dumps(item.get("evidence") or {}, separators=(",", ":"))) for item in analyzer.state_segments],
    )
    used = set()
    for event in analyzer.events:
        file_id = _file_for_timestamp(files, event["timestamp_ns"])
        match = None
        for old in sorted(previous, key=lambda old: (abs(old["timestamp_ns"] - event["timestamp_ns"]), old["id"])):
            if old["id"] in used or old["event_type"] != event["event_type"] or old["run_file_id"] != file_id:
                continue
            if abs(old["timestamp_ns"] - event["timestamp_ns"]) <= 1_000_000_000:
                match = old; used.add(old["id"]); break
        if match:
            conn.execute("update events set timestamp_ns=?,end_time_ns=?,detected_reason=?,auto_description=?,diagnostic_json=? where id=?",
                         (event["timestamp_ns"], event.get("end_time_ns"), event["reason"], event["auto_description"],
                          json.dumps(event.get("diagnostic") or {}), match["id"]))
            continue
        conn.execute(
            """insert into events(run_id,run_file_id,timestamp_ns,end_time_ns,event_type,detected_reason,
               effective_reason,source,auto_description,user_description,diagnostic_json,ignored,ignore_reason)
               values(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, file_id, event["timestamp_ns"], event.get("end_time_ns"), event["event_type"], event["reason"],
             match["effective_reason"] if match else None, "auto", event["auto_description"],
             match["user_description"] if match else None,
             json.dumps(event.get("diagnostic") or {}, separators=(",", ":")),
             match["ignored"] if match else 0, match["ignore_reason"] if match else None),
        )
    for old in previous:
        if old["id"] not in used:
            conn.execute("delete from events where id=?", (old["id"],))
    from .perfboard_voice import match_voice
    from .perfboard_voice import voice_file
    for row in conn.execute("select id,timestamp_ns from events where run_id=? and source='voice'", (run_id,)).fetchall():
        conn.execute("update events set run_file_id=? where id=?", (voice_file(files, row['timestamp_ns']), row['id']))
    match_voice(conn, run_id)
    conn.execute(
        """update runs set start_time_ns=?,end_time_ns=?,distance_m=?,gps_point_count=?,warning_summary=?,
           analysis_version=? where id=?""",
        (analyzer.start_ns, analyzer.end_ns, analyzer.distance_m, len(analyzer.gps_points),
         "\n".join(analyzer.warnings) or None, ANALYSIS_VERSION, run_id),
    )
    conn.commit()


def get_trajectory(conn, run_id: int, limit: int = 5000):
    rows = [dict(row) for row in conn.execute(
        "select timestamp_ns,lon,lat,height from gps_points where run_id=? order by timestamp_ns", (run_id,)
    )]
    if len(rows) <= limit:
        return rows
    indices = sorted({round(index * (len(rows) - 1) / (limit - 1)) for index in range(limit)})
    return [rows[index] for index in indices]


def get_series(conn, run_id: int, signal_key: str):
    run = get_run(conn, run_id)
    if not run:
        raise ValueError("run not found")
    rows = [dict(row) for row in conn.execute(
        "select timestamp_ns,value from signal_events where run_id=? and signal_key=? order by timestamp_ns",
        (run_id, signal_key),
    )]
    if signal_key != "closed_loop_active":
        return {"signal": signal_key, "available": bool(rows), "points": rows}
    available = bool(rows)
    if run["start_time_ns"] is None or run["end_time_ns"] is None:
        return {"signal": signal_key, "available": False, "points": []}
    points = []
    if not rows or rows[0]["timestamp_ns"] > run["start_time_ns"]:
        points.append({"timestamp_ns": run["start_time_ns"], "value": 0.0})
    points.extend(rows)
    if points[-1]["timestamp_ns"] != run["end_time_ns"]:
        points.append({"timestamp_ns": run["end_time_ns"], "value": points[-1]["value"]})
    return {"signal": signal_key, "available": available, "points": points}


def _decode_event(row):
    item = dict(row)
    item["ignored"] = bool(item["ignored"])
    item["reason"] = item["effective_reason"] or item["detected_reason"] or "manual_note"
    if item["source"] == "auto" and item["detected_reason"] == "longitudinal_override_started":
        if not item["effective_reason"]:
            item["reason"] = "longitudinal_override"
        item["auto_description"] = "Longitudinal override"
    # Card categories are derived rather than persisted. Voice reports start as
    # manual notes; enrich_events inherits a category only from a valid link.
    item["category"] = "manual_note" if item["source"] == "voice" else item["reason"]
    item["diagnostic"] = json.loads(item.pop("diagnostic_json") or "{}")
    return item


def _decode_events(rows):
    return [_decode_event(row) for row in rows
            if not (row["source"] == "auto" and row["detected_reason"] == "longitudinal_override_ended")]


def get_file_events(conn, file_id: int):
    row = conn.execute('select run_id from run_files where id=?', (file_id,)).fetchone()
    if not row:
        return []
    return [e for e in get_timeline(conn, row['run_id'])['events'] if e['run_file_id'] == file_id]


def get_timeline(conn, run_id: int):
    from .perfboard_voice import enrich_events
    run = get_run(conn, run_id)
    if not run:
        raise ValueError("run not found")
    version = int(run.get("analysis_version") or 1)
    if version < ANALYSIS_VERSION:
        series = get_series(conn, run_id, "closed_loop_active")
        points = series["points"]
        events = _decode_events(conn.execute(
            "select * from events where run_id=? order by timestamp_ns,id", (run_id,)
        ))
        enrich_events(conn, events)
        segments = []
        for index in range(len(points) - 1):
            if points[index + 1]["timestamp_ns"] > points[index]["timestamp_ns"]:
                segments.append({"start_time_ns": points[index]["timestamp_ns"],
                                 "end_time_ns": points[index + 1]["timestamp_ns"],
                                 "state": "active" if points[index]["value"] > .5 else "inactive", "reason": None})
        return {"analysis_version": version, "legacy": True, "available": series["available"],
                "segments": segments, "events": events,
                "summary": _timeline_summary(segments, events, _closed_loop_distance(conn, run_id, segments))}
    segments = [dict(row) for row in conn.execute(
        "select start_time_ns,end_time_ns,state,reason from state_segments where run_id=? order by start_time_ns", (run_id,)
    )]
    events = _decode_events(conn.execute(
        "select * from events where run_id=? order by timestamp_ns,id", (run_id,)
    ))
    enrich_events(conn, events)
    return {"analysis_version": version, "legacy": False, "available": bool(segments),
            "segments": segments, "events": events,
            "summary": _timeline_summary(segments, events, _closed_loop_distance(conn, run_id, segments))}


def _closed_loop_distance(conn, run_id, segments):
    points = conn.execute(
        "select timestamp_ns,lon,lat from gps_points where run_id=? order by timestamp_ns", (run_id,)
    ).fetchall()
    if len(points) < 2 or not segments:
        return 0.0
    segment_index = 0
    distance_m = 0.0
    for before, after in zip(points, points[1:]):
        midpoint_ns = (before["timestamp_ns"] + after["timestamp_ns"]) // 2
        while segment_index < len(segments) and segments[segment_index]["end_time_ns"] <= midpoint_ns:
            segment_index += 1
        if segment_index >= len(segments):
            break
        segment = segments[segment_index]
        if segment["start_time_ns"] <= midpoint_ns < segment["end_time_ns"] and segment["state"] in CLOSED_STATES:
            lat1, lat2 = math.radians(before["lat"]), math.radians(after["lat"])
            dlat = lat2 - lat1
            dlon = math.radians(after["lon"] - before["lon"])
            hav = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
            distance_m += 6_371_008.8 * 2 * math.atan2(math.sqrt(hav), math.sqrt(max(0.0, 1 - hav)))
    return distance_m


def _timeline_summary(segments, events, closed_loop_distance_m=0.0):
    active_ns = sum(item["end_time_ns"] - item["start_time_ns"] for item in segments if item["state"] in CLOSED_STATES)
    override_ns = sum(item["end_time_ns"] - item["start_time_ns"] for item in segments if item["state"] == "longitudinal_override")
    exits = [item for item in events if item.get("event_type") == "closed_loop_exit"]
    reasons = [item.get("reason") for item in exits if not item.get("ignored")]
    totals = [item.get("reason") for item in exits]
    return {
        "closed_loop_duration_s": active_ns / 1e9,
        "closed_loop_distance_m": closed_loop_distance_m,
        "longitudinal_override_duration_s": override_ns / 1e9,
        "longitudinal_override_count": sum(item.get("reason") == "longitudinal_override" and item.get("source") == "auto" for item in events),
        "brake_takeovers": reasons.count("brake_takeover"),
        "brake_takeovers_total": totals.count("brake_takeover"),
        "brake_takeovers_ignored": totals.count("brake_takeover") - reasons.count("brake_takeover"),
        "steering_takeovers": reasons.count("steering_takeover"),
        "steering_takeovers_total": totals.count("steering_takeover"),
        "steering_takeovers_ignored": totals.count("steering_takeover") - reasons.count("steering_takeover"),
        "other_exits": len(reasons) - reasons.count("brake_takeover") - reasons.count("steering_takeover"),
    }


def create_manual_event(conn, file_id: int, timestamp_ns: int, reason: str, description: str):
    file_row = conn.execute("select * from run_files where id=?", (file_id,)).fetchone()
    if not file_row:
        raise ValueError("record file not found")
    if file_row["begin_time_ns"] is not None and not (file_row["begin_time_ns"] <= timestamp_ns <= file_row["end_time_ns"]):
        raise ValueError("event time must be within the record time range")
    description = description.strip()
    if not description:
        raise ValueError("event description is required")
    if reason not in EVENT_REASONS:
        raise ValueError("invalid event reason")
    cur = conn.execute(
        """insert into events(run_id,run_file_id,timestamp_ns,event_type,detected_reason,effective_reason,
           source,user_description,diagnostic_json) values(?,?,?,?,?,?, 'manual',?, '{}')""",
        (file_row["run_id"], file_id, timestamp_ns, "manual_note", reason, reason, description),
    )
    conn.commit()
    return _decode_event(conn.execute("select * from events where id=?", (cur.lastrowid,)).fetchone())


def update_event(conn, event_id: int, *, timestamp_ns=None, reason=None, description=None, ignored=None, ignore_reason=None):
    event = conn.execute("select * from events where id=?", (event_id,)).fetchone()
    if not event:
        raise ValueError("event not found")
    values = {}
    if ignored is not None or ignore_reason is not None:
        eligible = event["source"] == "auto" and event["event_type"] == "closed_loop_exit" and (
            event["detected_reason"] in ("brake_takeover", "steering_takeover") or event["ignored"])
        if not eligible:
            raise ValueError("only automatic brake or steering takeovers can be ignored")
        if ignored is not None and not isinstance(ignored, bool):
            raise ValueError("ignored must be a boolean")
        if ignore_reason is not None and not isinstance(ignore_reason, str):
            raise ValueError("ignore reason must be text")
        next_ignored = event["ignored"] if ignored is None else ignored
        note = (event["ignore_reason"] or "") if ignore_reason is None else ignore_reason.strip()
        if next_ignored and not note.strip():
            raise ValueError("ignore reason is required")
        if ignored is not None:
            values["ignored"] = int(ignored)
        if ignore_reason is not None:
            values["ignore_reason"] = note or None
    if reason is not None:
        if reason and reason not in EVENT_REASONS:
            raise ValueError("invalid event reason")
        values["effective_reason"] = reason or None
    if description is not None:
        values["user_description"] = str(description).strip() or None
    if timestamp_ns is not None:
        if event["source"] not in ("manual", "voice"):
            raise ValueError("automatic event time cannot be changed")
        file_row = conn.execute("select * from run_files where id=?", (event["run_file_id"],)).fetchone()
        if event["source"] == "manual" and file_row and file_row["begin_time_ns"] is not None and not (file_row["begin_time_ns"] <= timestamp_ns <= file_row["end_time_ns"]):
            raise ValueError("event time must be within the record time range")
        values["timestamp_ns"] = int(timestamp_ns)
        if event["source"] == "voice":
            from .perfboard_voice import voice_file
            values["run_file_id"] = voice_file(get_run_files(conn, event["run_id"]), int(timestamp_ns))
    if values:
        assignments = ",".join(f"{key}=?" for key in values)
        conn.execute(f"update events set {assignments},updated_at=current_timestamp where id=?", (*values.values(), event_id))
        from .perfboard_voice import match_voice
        match_voice(conn, event["run_id"])
        conn.commit()
    updated = _decode_event(conn.execute("select * from events where id=?", (event_id,)).fetchone())
    if updated["source"] == "voice":
        # Return the same derived category as timeline/file APIs, including any
        # association changed by an edited report time.
        return next(item for item in get_timeline(conn, updated["run_id"])["events"] if item["id"] == event_id)
    return updated


def delete_or_clear_event(conn, event_id: int):
    event = conn.execute("select * from events where id=?", (event_id,)).fetchone()
    if not event:
        raise ValueError("event not found")
    if event["source"] in ("manual", "voice"):
        conn.execute("delete from events where id=?", (event_id,))
        result = {"id": event_id, "deleted": True}
    else:
        conn.execute("update events set effective_reason=null,user_description=null,updated_at=current_timestamp where id=?", (event_id,))
        result = {"id": event_id, "deleted": False, "annotations_cleared": True}
    conn.commit()
    return result


def reset_for_retry(conn, run_id: int):
    run = get_run(conn, run_id)
    if not run:
        raise ValueError("run not found")
    if run["status"] in ACTIVE_STATUSES:
        raise ValueError("run is already active")
    conn.execute("update run_files set status='pending',error=null where run_id=? and status='failed'", (run_id,))
    conn.execute("update runs set status='queued',error_summary=null,current_file=null,completed_at=null where id=?", (run_id,))
    conn.commit()


def delete_run_row(conn, run_id: int):
    conn.execute("delete from runs where id=?", (run_id,))
    conn.commit()
