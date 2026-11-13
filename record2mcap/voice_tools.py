"""CLI helpers shared by the event-extraction skill and offline debugging."""
import argparse
import hashlib
import json
from pathlib import Path
from .perfboard_voice import normalize_transcript, probe, time_ns, tool_paths, validate_events


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command', required=True)
    norm = sub.add_parser('normalize')
    norm.add_argument('--audio', required=True)
    norm.add_argument('--raw', required=True)
    norm.add_argument('--recording-id', required=True)
    norm.add_argument('--start-time')
    norm.add_argument('--output', required=True)
    final = sub.add_parser('finalize')
    final.add_argument('--transcript', required=True)
    final.add_argument('--events', required=True)
    final.add_argument('--output', required=True)
    args = parser.parse_args()
    if args.command == 'normalize':
        audio = Path(args.audio)
        _, ffmpeg = tool_paths()
        duration, raw_start = probe(ffmpeg, audio)
        digest = hashlib.sha256()
        with audio.open('rb') as f:
            for chunk in iter(lambda: f.read(1024*1024), b''):
                digest.update(chunk)
        rec = dict(id=args.recording_id, filename=audio.name, sha256=digest.hexdigest(), duration_ms=duration,
                   start_time_ns=time_ns(args.start_time or raw_start), raw_start_time=raw_start,
                   time_source='manual' if args.start_time else 'metadata')
        result = normalize_transcript(rec, json.loads(Path(args.raw).read_text()))
    else:
        transcript = json.loads(Path(args.transcript).read_text())
        result = json.loads(Path(args.events).read_text())
        result['events'] = validate_events(result, transcript)
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"Saved {args.output}")


if __name__ == '__main__':
    main()
