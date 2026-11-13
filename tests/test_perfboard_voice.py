import io
import json
import sqlite3
import tempfile
import threading
import urllib.request
import urllib.error
import unittest
from pathlib import Path
from unittest.mock import patch
from record2mcap import perfboard_voice as v
from record2mcap.perfboard_storage import connect, init_db, get_timeline, get_file_events, create_manual_event, update_event, delete_or_clear_event, save_analysis
from record2mcap.perfboard_analysis import RunAnalyzer
from record2mcap.perfboard_server import Handler
from http.server import ThreadingHTTPServer


class VoiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.conn = connect(self.root / 'db.sqlite')
        init_db(self.conn)
        self.ns = v.time_ns('2026-09-27T13:29:11+08:00')
        self.conn.execute("insert into runs(id,name,version_tag,data_path,status,start_time_ns,end_time_ns) values(1,'Test','v','test','ready',?,?)", (self.ns, self.ns+60_000_000_000))
        self.conn.execute("insert into run_files(id,run_id,source_path,output_path,source_size,begin_time_ns,end_time_ns) values(1,1,'a','b',1,?,?)", (self.ns, self.ns+60_000_000_000))
        self.conn.execute("insert into recordings(id,run_id,filename,sha256,directory,status,duration_ms,start_time_ns) values('rec',1,'audio.m4a','hash',?,'ready',60000,?)", (str(self.root), self.ns))
        self.transcript = v.normalize_transcript(dict(id='rec',sha256='hash',filename='a',duration_ms=60000,start_time_ns=self.ns,raw_start_time=None,time_source='manual'),
            {'segments':[{'start':10,'end':15,'text':'刚才刹车接管'},{'start':20,'end':23,'text':'没有接管'}]})
        (self.root/'transcript.json').write_text(json.dumps(self.transcript))
        self.conn.commit()

    def tearDown(self):
        self.conn.close(); self.temp.cleanup()

    def payload(self, takeover='brake', refs=None):
        return dict(schema_version=1,kind='perfboard_voice_events',recording_id='rec',sha256='hash',events=[
            dict(id='v1',category='issue',title='Issue',description='Description',reason='other',takeover=takeover,segment_ids=refs or ['s0000'])])

    def exit(self, seconds, reason='brake_takeover'):
        cur=self.conn.execute("insert into events(run_id,run_file_id,timestamp_ns,event_type,source,detected_reason) values(1,1,?,'closed_loop_exit','auto',?)", (self.ns+seconds*1_000_000_000,reason))
        self.conn.commit();return cur.lastrowid

    def evidence(self):
        return dict(self.conn.execute('select * from voice_evidence').fetchone())

    def event(self, event_id):
        return next(e for e in get_timeline(self.conn, 1)['events'] if e['id'] == event_id)

    def test_unlinked_voice_category_is_manual_note_for_every_assessment(self):
        payload = self.payload('none')
        payload['events'] = [dict(payload['events'][0], id=category, category=category,
                                  reason='brake_takeover') for category in ('issue', 'check', 'positive')]
        v.import_events(self.conn, 'rec', payload)
        for event in get_file_events(self.conn, 1):
            self.assertEqual(event['category'], 'manual_note')
            self.assertEqual(event['reason'], 'brake_takeover')
            self.assertEqual(event['voice']['category'], event['voice']['external_id'])
            self.assertEqual(event['voice']['match_status'], 'not_applicable')
        self.assertEqual(get_timeline(self.conn, 1)['summary']['brake_takeovers'], 0)

    def test_linked_category_follows_effective_automatic_category_and_clear(self):
        target = self.exit(8)
        v.import_events(self.conn, 'rec', self.payload())
        ev = self.evidence()['event_id']
        self.assertEqual(self.event(target)['category'], 'brake_takeover')
        self.assertEqual(self.event(ev)['category'], 'brake_takeover')
        self.assertEqual(self.event(ev)['voice']['category'], 'issue')
        updated = update_event(self.conn, target, reason='steering_takeover')
        self.assertEqual(updated['category'], 'steering_takeover')
        report = self.event(ev)
        self.assertEqual(report['category'], 'steering_takeover')
        self.assertEqual(report['reason'], 'other')
        self.assertEqual(report['voice']['category'], 'issue')
        self.assertEqual(report['voice']['takeover'], 'brake')
        self.assertEqual(report['link_candidates'][0]['category'], 'steering_takeover')
        self.assertEqual(self.event(target)['voice_reports'][0]['category'], 'steering_takeover')
        self.assertEqual(update_event(self.conn, ev, description='Edited report')['category'], 'steering_takeover')
        delete_or_clear_event(self.conn, target)
        self.assertEqual(self.event(ev)['category'], 'brake_takeover')

    def test_unlink_manual_link_and_rematch_rederive_category(self):
        target = self.exit(8)
        v.import_events(self.conn, 'rec', self.payload())
        ev = self.evidence()['event_id']
        v.set_link(self.conn, ev, mode='disabled')
        self.assertEqual(self.event(ev)['category'], 'manual_note')
        self.assertNotIn('voice_reports', self.event(target))
        v.set_link(self.conn, ev, target)
        self.assertEqual(self.event(ev)['category'], 'brake_takeover')
        v.set_link(self.conn, ev, mode='auto')
        self.assertEqual(self.event(ev)['category'], 'brake_takeover')
        self.exit(9)
        v.match_voice(self.conn, 1)
        self.assertEqual(self.event(ev)['voice']['match_status'], 'ambiguous')
        self.assertEqual(self.event(ev)['category'], 'manual_note')

    def test_stale_or_invalid_links_do_not_inherit_or_attach(self):
        target = self.exit(8)
        v.import_events(self.conn, 'rec', self.payload())
        ev = self.evidence()['event_id']
        for status, mode in (('unmatched', 'auto'), ('ambiguous', 'auto'), ('linked', 'disabled')):
            with self.subTest(status=status, mode=mode):
                self.conn.execute('update voice_evidence set linked_event_id=?,match_status=?,link_mode=? where event_id=?',
                                  (target, status, mode, ev))
                self.assertEqual(self.event(ev)['category'], 'manual_note')
                self.assertIsNone(self.event(ev)['voice']['delta_s'])
                self.assertNotIn('voice_reports', self.event(target))
        manual = create_manual_event(self.conn, 1, self.ns + 8_000_000_000, 'steering_takeover', 'Manual')
        self.conn.execute("update voice_evidence set linked_event_id=?,match_status='linked',link_mode='manual' where event_id=?",
                          (manual['id'], ev))
        self.assertEqual(self.event(ev)['category'], 'manual_note')
        self.assertNotIn('voice_reports', self.event(manual['id']))
        self.conn.execute('delete from events where id=?', (manual['id'],))
        self.assertIsNone(self.event(ev)['voice']['linked_event_id'])
        self.assertEqual(self.event(ev)['category'], 'manual_note')

    def test_reimport_preserves_report_assessment_reason_and_disabled_link(self):
        self.exit(8)
        v.import_events(self.conn, 'rec', self.payload())
        ev = self.evidence()['event_id']
        v.set_link(self.conn, ev, mode='disabled')
        payload = self.payload()
        payload['events'][0].update(category='positive', reason='steering_takeover', title='Replacement')
        self.assertEqual(v.import_events(self.conn, 'rec', payload), {'added': 0, 'skipped': 1})
        report = self.event(ev)
        self.assertEqual(report['category'], 'manual_note')
        self.assertEqual(report['reason'], 'other')
        self.assertEqual(report['voice']['category'], 'issue')
        self.assertEqual(report['voice']['title'], 'Issue')
        self.assertEqual(report['voice']['link_mode'], 'disabled')

    def test_identity_validation_atomic_duplicate(self):
        bad=self.payload();bad['sha256']='wrong'
        with self.assertRaises(ValueError):v.import_events(self.conn,'rec',bad)
        bad=self.payload();bad['events'].append(dict(bad['events'][0],id='v2',segment_ids=['missing']))
        with self.assertRaises(ValueError):v.import_events(self.conn,'rec',bad)
        self.assertEqual(self.conn.execute('select count(*) from voice_evidence').fetchone()[0],0)
        self.assertEqual(v.import_events(self.conn,'rec',self.payload())['added'],1)
        self.assertEqual(v.import_events(self.conn,'rec',self.payload())['skipped'],1)

    def test_matching_types_delay_and_statistics(self):
        target=self.exit(8);self.exit(10,'steering_takeover')
        v.import_events(self.conn,'rec',self.payload())
        self.assertEqual(self.evidence()['linked_event_id'],target)
        events=get_timeline(self.conn,1)['events']
        automatic=next(e for e in events if e['id']==target)
        self.assertEqual(automatic['voice_reports'][0]['voice']['delta_s'],2)
        self.assertEqual(get_timeline(self.conn,1)['summary']['brake_takeovers'],1)
        ev=self.evidence()['event_id']
        v.set_link(self.conn,ev,mode='disabled');v.match_voice(self.conn,1)
        self.assertIsNone(self.evidence()['linked_event_id'])
        v.set_link(self.conn,ev,mode='auto')
        self.assertEqual(self.evidence()['linked_event_id'],target)

    def test_ambiguity_none_and_type_conflict(self):
        self.exit(8);self.exit(9)
        v.import_events(self.conn,'rec',self.payload())
        self.assertEqual(self.evidence()['match_status'],'ambiguous')
        self.assertEqual(self.event(self.evidence()['event_id'])['category'],'manual_note')
        self.conn.execute("update voice_evidence set takeover='none'");v.match_voice(self.conn,1)
        self.assertIsNone(self.evidence()['linked_event_id'])
        self.assertEqual(self.event(self.evidence()['event_id'])['category'],'manual_note')
        self.conn.execute("update voice_evidence set takeover='steering'");v.match_voice(self.conn,1)
        self.assertEqual(self.evidence()['match_status'],'type_conflict')
        self.assertEqual(self.event(self.evidence()['event_id'])['category'],'manual_note')

    def test_bounds_manual_link_and_independent_audio_time(self):
        target=self.exit(-20) # exactly 30 s before speech
        v.import_events(self.conn,'rec',self.payload())
        self.assertEqual(self.evidence()['linked_event_id'],target)
        ev=self.evidence()['event_id'];v.set_link(self.conn,ev,target)
        update_event(self.conn,ev,timestamp_ns=self.ns+100_000_000_000,description='Edited')
        row=self.conn.execute('select * from events where id=?',(ev,)).fetchone()
        self.assertIsNone(row['run_file_id'])
        self.assertEqual(self.evidence()['start_ms'],10000)
        self.assertEqual(self.evidence()['linked_event_id'],target)
        delete_or_clear_event(self.conn,ev)
        self.assertIsNotNone(self.conn.execute('select * from events where id=?',(target,)).fetchone())

    def test_bad_manual_link(self):
        v.import_events(self.conn,'rec',self.payload())
        with self.assertRaises(ValueError):v.set_link(self.conn,self.evidence()['event_id'],999)

    def test_reanalysis_preserves_link_and_removes_missing_exit(self):
        target=self.exit(8)
        v.import_events(self.conn,'rec',self.payload())
        ev=self.evidence()['event_id'];v.set_link(self.conn,ev,target)
        analyzer=RunAnalyzer();analyzer.start_ns=self.ns;analyzer.end_ns=self.ns+60_000_000_000
        analyzer.events=[dict(timestamp_ns=self.ns+8_200_000_000,event_type='closed_loop_exit',reason='brake_takeover',auto_description='Brake')]
        save_analysis(self.conn,1,analyzer)
        self.assertEqual(self.evidence()['linked_event_id'],target)
        self.assertEqual(self.event(ev)['category'],'brake_takeover')
        analyzer.events=[];save_analysis(self.conn,1,analyzer)
        self.assertIsNone(self.evidence()['linked_event_id'])
        self.assertEqual(self.evidence()['link_mode'],'auto')
        self.assertEqual(self.event(ev)['category'],'manual_note')

    def test_matching_window_late_edge_and_multiple_reports(self):
        target=self.exit(15) # exactly 5 s after speech
        payload=self.payload();payload['events'].append(dict(payload['events'][0],id='v2'))
        v.import_events(self.conn,'rec',payload)
        self.assertEqual(self.conn.execute('select count(*) from voice_evidence where linked_event_id=?',(target,)).fetchone()[0],2)
        self.conn.execute('update events set timestamp_ns=? where id=?',(self.ns+15_000_000_001,target));v.match_voice(self.conn,1)
        self.assertIsNone(self.evidence()['linked_event_id'])

    def test_http_import_upload_and_range(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        server.db_path=self.root/'db.sqlite'
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base=f'http://127.0.0.1:{server.server_port}'
        try:
            (self.root/'playback.m4a').write_bytes(b'0123456789')
            req=urllib.request.Request(base+'/api/recordings/rec/audio',headers={'Range':'bytes=2-5'})
            with urllib.request.urlopen(req) as response:
                self.assertEqual(response.status,206);self.assertEqual(response.read(),b'2345')
                self.assertEqual(response.headers['Content-Range'],'bytes 2-5/10')
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(urllib.request.Request(base+'/api/recordings/rec/audio',headers={'Range':'bytes=99-'}))
            self.assertEqual(error.exception.code,416)
            req=urllib.request.Request(base+'/api/recordings/rec/events',data=json.dumps(self.payload()).encode(),headers={'Content-Type':'application/json'})
            self.assertEqual(json.load(urllib.request.urlopen(req))['added'],1)
            timeline=json.load(urllib.request.urlopen(base+'/api/runs/1/timeline'))
            report=next(e for e in timeline['events'] if e['source']=='voice')
            self.assertEqual(report['category'],'manual_note')
            self.assertEqual(report['voice']['category'],'issue')
            file_events=json.load(urllib.request.urlopen(base+'/api/run-files/1/events'))
            self.assertEqual(file_events[0]['category'],'manual_note')
            target=self.exit(8);v.set_link(self.conn,report['id'],target)
            req=urllib.request.Request(base+f"/api/events/{report['id']}",data=json.dumps({'description':'HTTP edit'}).encode(),
                                       headers={'Content-Type':'application/json'},method='PATCH')
            self.assertEqual(json.load(urllib.request.urlopen(req))['category'],'brake_takeover')
            with patch.object(v,'tool_paths',return_value=('python','ffmpeg')),patch.object(v,'probe',return_value=(2000,None)):
                req=urllib.request.Request(base+'/api/runs/1/recordings',data=b'test audio',headers={'X-Filename':'sample.wav'})
                uploaded=json.load(urllib.request.urlopen(req));self.assertIsNone(uploaded['start_time_ns'])
                self.assertEqual(uploaded['status'],'uploaded')
                self.assertEqual(Path(uploaded['directory'],'original.wav').read_bytes(),b'test audio')
        finally:
            server.shutdown();server.server_close();thread.join()

    def test_time_precision(self):
        self.assertEqual(v.time_ns('2026-09-27T05:29:11Z'),self.ns)
        events=v.validate_events(self.payload(),self.transcript)
        self.assertEqual(events[0]['timestamp_ns'],str(self.ns+10_000_000_000))
        with self.assertRaises(ValueError):v.time_ns('2026-09-27T13:29:11')

    def test_voice_override_does_not_increment_signal_statistics(self):
        payload=self.payload('none');payload['events'][0]['reason']='longitudinal_override'
        v.import_events(self.conn,'rec',payload)
        self.assertEqual(get_timeline(self.conn,1)['summary']['longitudinal_override_count'],0)

    def test_legacy_migration(self):
        conn=sqlite3.connect(':memory:');conn.row_factory=sqlite3.Row
        conn.executescript("create table runs(id integer primary key); create table run_files(id integer primary key); create table events(id integer primary key,run_id integer,run_file_id integer,timestamp_ns integer,source text check(source in ('auto','manual'))); insert into events values(42,1,null,1,'manual');")
        v.init_voice(conn)
        self.assertEqual(conn.execute('select id from events').fetchone()[0],42)
        conn.execute("insert into events values(43,1,null,2,'voice')")
        self.assertEqual(conn.execute('pragma foreign_keys').fetchone()[0],1)
        conn.close()


if __name__=='__main__':unittest.main()
