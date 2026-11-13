"""Optional transcription must not prevent the basic application from running."""
import json
import sqlite3
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.request import urlopen
from http.server import ThreadingHTTPServer

from record2mcap import perfboard_voice as voice
from record2mcap.perfboard_server import Handler
from record2mcap.perfboard_storage import connect, open_db, init_db


class OptionalVoiceTests(unittest.TestCase):
    def test_database_context_closes_handles_and_rolls_back_failures(self):
        with tempfile.TemporaryDirectory() as root:
            db = Path(root) / 'context.sqlite3'
            with open_db(db) as conn:
                conn.execute('create table notes (value text)')
                conn.execute("insert into notes values ('saved')")
            with self.assertRaises(sqlite3.ProgrammingError):
                conn.execute('select 1')
            with self.assertRaisesRegex(ValueError, 'rollback'):
                with open_db(db) as conn:
                    conn.execute("insert into notes values ('discarded')")
                    raise ValueError('rollback')
            with self.assertRaises(sqlite3.ProgrammingError):
                conn.execute('select 1')
            with open_db(db) as conn:
                self.assertEqual([row[0] for row in conn.execute('select value from notes')], ['saved'])

    def test_missing_whisper_leaves_core_http_and_static_assets_available(self):
        with tempfile.TemporaryDirectory() as root:
            db = Path(root) / 'perfboard.sqlite3'
            with open_db(db) as conn:
                init_db(conn)
            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            server.db_path = db
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f'http://127.0.0.1:{server.server_port}'
            try:
                with patch.object(voice, 'tool_paths', side_effect=ValueError('No Whisper')):
                    with urlopen(base + '/api/capabilities') as response:
                        capability = json.load(response)['local_transcription']
                    self.assertFalse(capability['available'])
                    self.assertIn('optional', capability['message'])
                    with urlopen(base + '/api/runs') as response:
                        self.assertEqual(json.load(response), [])
                    for asset in ('/', '/app.js', '/voice.js', '/styles.css'):
                        with urlopen(base + asset) as response:
                            self.assertEqual(response.status, 200)
                            self.assertTrue(response.read())
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
