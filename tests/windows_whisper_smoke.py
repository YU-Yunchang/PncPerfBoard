"""Exercise audio ingest, local Whisper, transcript conversion and audio output."""
import json
import os
from pathlib import Path
import sys
import tempfile

from record2mcap import perfboard_voice as voice
from record2mcap.perfboard_storage import create_run, init_db, open_db


def main(audio):
    os.environ['PERFBOARD_WHISPER_LANGUAGE'] = 'en'
    with tempfile.TemporaryDirectory() as directory:
        db = Path(directory) / 'test.sqlite3'
        with open_db(db) as connection:
            init_db(connection)
            run_id = create_run(connection, 'Smoke run', 'ci', directory, [])
            with audio.open('rb') as stream:
                rec = voice.register_recording(connection, db, run_id, audio.name, stream,
                                               audio.stat().st_size, '2026-01-01T00:00:00Z')
        voice.VoiceQueue(db).process(rec['id'])
        output = Path(rec['directory'])
        transcript = json.loads((output / 'transcript.json').read_text(encoding='utf-8'))
        assert transcript['model'] == 'Systran/faster-whisper-small', transcript
        assert transcript['segments'] and any(s['text'] for s in transcript['segments']), transcript
        assert (output / 'playback.m4a').stat().st_size > 0
        first = transcript['segments'][0]
        payload = dict(schema_version=1, kind='perfboard_voice_events',
                       recording_id=rec['id'], sha256=rec['sha256'], events=[
                           dict(id='ci-note', category='check', title='Smoke check',
                                description='Synthetic speech', reason='other', takeover='none',
                                segment_ids=[first['id']]),
                       ])
        with open_db(db) as connection:
            assert voice.recording(connection, rec['id'])['status'] == 'ready'
            assert voice.import_events(connection, rec['id'], payload)['added'] == 1


if __name__ == '__main__':
    main(Path(sys.argv[1]))
