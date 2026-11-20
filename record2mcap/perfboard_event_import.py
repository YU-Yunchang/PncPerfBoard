"""Import timestamped field notes into a Run without a recording."""
import json

from .perfboard_storage import EVENT_REASONS, get_run, get_run_files
from .perfboard_voice import time_ns


def is_manual_batch(payload):
    if not isinstance(payload, dict) or {"recording_id", "sha256"}.intersection(payload):
        return False
    if "kind" in payload:
        return payload["kind"] == "perfboard_manual_events"
    # The original screenshot export had a timezone but no kind.
    return isinstance(payload.get("timezone"), str)


def import_manual_batch(conn, run_id, payload):
    if not is_manual_batch(payload) or payload.get("schema_version") != 1:
        raise ValueError("Choose a manual event JSON file for this Run. Import voice events on their recording.")
    rows = payload.get("events")
    if not isinstance(rows, list) or not rows:
        raise ValueError("The file must contain a nonempty events array.")

    added = 0
    with conn:
        # Serialize duplicate checks, including simultaneous uploads from two tabs.
        conn.execute("begin immediate")
        if not get_run(conn, run_id):
            raise ValueError("Run not found.")
        files = get_run_files(conn, run_id)
        previous = {}
        for event in conn.execute("select * from events where run_id=? and source='manual'", (run_id,)):
            metadata = json.loads(event["diagnostic_json"] or "{}").get("manual_report")
            key = ((metadata["timestamp_ns"], metadata["description"]) if metadata else
                   (str(event["timestamp_ns"]), event["user_description"]))
            previous[key] = (event["id"], bool(metadata))

        for number, row in enumerate(rows, 1):
            if not isinstance(row, dict) or "segment_ids" in row:
                raise ValueError(f"Event {number} must be a manual event.")
            category = row.get("category")
            takeover = row.get("takeover", "none")
            reason = row.get("reason", "manual_note")
            description = row.get("description")
            if category not in ("issue", "check", "positive") or takeover not in ("brake", "steering", "unspecified", "none"):
                raise ValueError(f"Event {number} has an invalid category or takeover type.")
            if reason not in EVENT_REASONS or not isinstance(description, str) or not description.strip():
                raise ValueError(f"Event {number} needs a supported reason and a description.")
            raw_ns = row.get("timestamp_ns")
            if not isinstance(raw_ns, str) or not raw_ns.isascii() or not raw_ns.isdigit() or len(raw_ns) > 19:
                raise ValueError(f"Event {number} needs a decimal timestamp_ns string.")
            ns = int(raw_ns)
            if "time" in row and time_ns(row["time"]) != ns:
                raise ValueError(f"Event {number}: time and timestamp_ns disagree.")
            description = description.strip()
            key = (str(ns), description)
            metadata = dict(category=category, takeover=takeover, scene=row.get("scene"),
                            timestamp_ns=str(ns), description=description)
            diagnostic = json.dumps({"manual_report": metadata}, ensure_ascii=False)
            if key in previous:
                event_id, classified = previous[key]
                if not classified:
                    # Upgrade matching manual notes created by the original companion script.
                    conn.execute("update events set diagnostic_json=? where id=?", (diagnostic, event_id))
                    previous[key] = (event_id, True)
                continue
            matches = [f["id"] for f in files if f["begin_time_ns"] is not None and f["end_time_ns"] is not None
                       and f["begin_time_ns"] <= ns <= f["end_time_ns"]]
            if len(matches) != 1:
                stamp = row.get("time", raw_ns)
                raise ValueError(f"Event {number} ({stamp}) matches {len(matches)} record files in this Run; expected one.")
            cursor = conn.execute(
                """insert into events(run_id,run_file_id,timestamp_ns,event_type,detected_reason,effective_reason,
                                      source,user_description,diagnostic_json)
                   values(?,?,?,'manual_note',?,?,'manual',?,?)""",
                (run_id, matches[0], ns, reason, reason, description, diagnostic))
            previous[key] = (cursor.lastrowid, True)
            added += 1
    return {"added": added, "skipped": len(rows) - added}
