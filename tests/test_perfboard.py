import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

from record2mcap.perfboard_analysis import RunAnalyzer, CONTROL_TOPIC, POSE_TOPIC, STATE_TOPIC, VEHICLE_TOPIC
from record2mcap.perfboard_jobs import output_for
from record2mcap.perfboard_server import parse_event_path, parse_file_open_path, parse_run_file_path, parse_run_path
from record2mcap.perfboard_storage import (
    connect, create_manual_event, create_run, delete_or_clear_event, get_file_events,
    get_series, get_timeline, get_trajectory, init_db, save_analysis, update_event,
)


class Pose(NS):
    def HasField(self, name):
        return name == "position_llh"


class PerfBoardAnalysisTests(unittest.TestCase):
    def vehicle(self, analyzer, ns, eps=1, override=0, brake=0, abort=0):
        analyzer.process(VEHICLE_TOPIC, NS(eps_lccontrolstatus=eps,
                                          vcu_indicateddriveroverride=override,
                                          vcu_brkpedalstasus=brake,
                                          eps_abortreason=abort), ns)

    def control(self, analyzer, ns, enabled=True, lat=False, lon=False):
        analyzer.process(CONTROL_TOPIC, NS(control_noa_enable=enabled,
                                           lat_override=lat, lon_override=lon), ns)

    def state(self, analyzer, ns, noa=4, reason_codes=()):
        analyzer.process(STATE_TOPIC, NS(NOA_State=noa, reason_code=list(reason_codes)), ns)

    def test_gps_is_sampled_at_five_hz_but_distance_uses_every_point(self):
        analyzer = RunAnalyzer()
        for index in range(11):
            analyzer.process(POSE_TOPIC, Pose(position_llh=NS(lon=index * 0.000001, lat=0., height=1.)),
                             index * 100_000_000)
        analyzer.finalize()
        self.assertEqual(len(analyzer.gps_points), 6)
        self.assertGreater(analyzer.distance_m, 1.0)
        self.assertEqual(analyzer.gps_points[-1][0], 1_000_000_000)

    def test_closed_loop_preserves_real_short_segments(self):
        analyzer = RunAnalyzer()
        for tick in range(10):
            ns = tick * 100_000_000
            self.vehicle(analyzer, ns); self.control(analyzer, ns); self.state(analyzer, ns)
        self.vehicle(analyzer, 1_000_000_000); self.control(analyzer, 1_000_000_000, enabled=False); self.state(analyzer, 1_000_000_000, noa=3)
        analyzer.observe_range(0, 1_100_000_000)
        analyzer.finalize()
        self.assertEqual(analyzer.closed_loop_events, [(0, 1.0), (1_000_000_000, 0.0)])
        self.assertEqual(analyzer.events[0]["reason"], "closed_loop_disabled")

    def test_feedback_gap_is_unknown_but_a_short_gap_is_merged(self):
        analyzer = RunAnalyzer()
        self.vehicle(analyzer, 0); self.control(analyzer, 0); self.state(analyzer, 0)
        self.control(analyzer, 300_000_000); self.state(analyzer, 300_000_000)
        self.vehicle(analyzer, 350_000_000); self.control(analyzer, 350_000_000); self.state(analyzer, 350_000_000)
        analyzer.finalize()
        self.assertEqual([item["state"] for item in analyzer.state_segments], ["active"])

    def test_longitudinal_override_is_closed_loop_and_brake_has_priority(self):
        analyzer = RunAnalyzer()
        self.vehicle(analyzer, 0); self.control(analyzer, 0); self.state(analyzer, 0)
        self.vehicle(analyzer, 100_000_000, override=1); self.control(analyzer, 100_000_000, lon=True); self.state(analyzer, 100_000_000)
        self.vehicle(analyzer, 200_000_000, override=1, brake=1); self.control(analyzer, 200_000_000, lat=True, lon=True); self.state(analyzer, 200_000_000, noa=3)
        analyzer.observe_range(0, 300_000_000)
        analyzer.finalize()
        self.assertEqual([item["state"] for item in analyzer.state_segments],
                         ["active", "longitudinal_override", "inactive"])
        self.assertEqual([item["reason"] for item in analyzer.events],
                         ["longitudinal_override", "brake_takeover"])
        self.assertEqual(analyzer.closed_loop_events, [(0, 1.0), (200_000_000, 0.0)])

    def test_takeover_reason_uses_priority_within_causal_window(self):
        analyzer = RunAnalyzer()
        for tick in range(10):
            ns = tick * 100_000_000
            self.vehicle(analyzer, ns); self.control(analyzer, ns); self.state(analyzer, ns)
        self.vehicle(analyzer, 1_000_000_000); self.control(analyzer, 1_000_000_000, enabled=False); self.state(analyzer, 1_000_000_000, noa=3)
        self.vehicle(analyzer, 1_100_000_000, brake=1); self.control(analyzer, 1_100_000_000, enabled=False); self.state(analyzer, 1_100_000_000, noa=3)
        analyzer.observe_range(0, 1_200_000_000); analyzer.finalize()
        exits = [item for item in analyzer.events if item["event_type"] == "closed_loop_exit"]
        self.assertEqual(exits[0]["reason"], "brake_takeover")

    def test_noa_state_is_authoritative_over_vehicle_and_control_signals(self):
        analyzer = RunAnalyzer()
        self.vehicle(analyzer, 0); self.control(analyzer, 0); self.state(analyzer, 0)
        self.vehicle(analyzer, 100_000_000, eps=2, brake=1, abort=2)
        self.control(analyzer, 100_000_000, enabled=False, lat=True)
        self.state(analyzer, 100_000_000, noa=4)
        analyzer.observe_range(0, 200_000_000); analyzer.finalize()
        self.assertEqual([item["state"] for item in analyzer.state_segments], ["active"])
        self.assertEqual(analyzer.events, [])

    def test_active_segment_does_not_extend_over_a_recording_gap(self):
        analyzer = RunAnalyzer()
        for tick in range(51):
            ns = tick * 100_000_000
            self.vehicle(analyzer, ns)
            self.control(analyzer, ns); self.state(analyzer, ns)
        analyzer.observe_range(0, 20_000_000_000)
        analyzer.finalize()
        self.assertEqual(analyzer.closed_loop_events, [(0, 1.0), (5_200_000_000, 0.0)])
        self.assertEqual(analyzer.state_segments[-1]["state"], "unknown")


class PerfBoardStorageTests(unittest.TestCase):
    def test_storage_reconstructs_series_and_limits_trajectory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "minute" / "gaea" / "a.record"
            source.parent.mkdir(parents=True); source.write_bytes(b"x")
            db = root / "perfboard.sqlite3"
            with connect(db) as conn:
                init_db(conn)
                run_id = create_run(conn, "run", "v1", str(root), [(source, output_for(source))])
                analyzer = RunAnalyzer(); analyzer.start_ns = 1; analyzer.end_ns = 10
                analyzer.gps_points = [(i, float(i), 1., None) for i in range(1, 10)]
                analyzer.closed_loop_events = [(3, 1.), (8, 0.)]
                analyzer.state_segments = [
                    {"start_time_ns": 1, "end_time_ns": 3, "state": "inactive", "reason": "closed_loop_disabled", "evidence": {}},
                    {"start_time_ns": 3, "end_time_ns": 8, "state": "longitudinal_override", "reason": None, "evidence": {}},
                    {"start_time_ns": 8, "end_time_ns": 10, "state": "inactive", "reason": "brake_takeover", "evidence": {}},
                ]
                analyzer.events = [{"timestamp_ns": 8, "end_time_ns": None, "event_type": "closed_loop_exit",
                                    "reason": "brake_takeover", "auto_description": "Brake takeover", "diagnostic": {}}]
                conn.execute("update run_files set begin_time_ns=1,end_time_ns=10 where run_id=?", (run_id,))
                save_analysis(conn, run_id, analyzer)
                self.assertEqual(len(get_trajectory(conn, run_id, limit=5)), 5)
                series = get_series(conn, run_id, "closed_loop_active")
                self.assertTrue(series["available"])
                self.assertEqual(series["points"][0], {"timestamp_ns": 1, "value": 0.0})
                self.assertEqual(series["points"][-1], {"timestamp_ns": 10, "value": 0.0})
                timeline = get_timeline(conn, run_id)
                self.assertFalse(timeline["legacy"])
                self.assertEqual(timeline["summary"]["brake_takeovers"], 1)
                self.assertEqual(timeline["summary"]["closed_loop_duration_s"], 5e-9)
                self.assertGreater(timeline["summary"]["closed_loop_distance_m"], 0)
                file_id = conn.execute("select id from run_files where run_id=?", (run_id,)).fetchone()[0]
                manual = create_manual_event(conn, file_id, 5, "manual_note", "Driver comment")
                self.assertEqual(manual["source"], "manual")
                self.assertEqual(manual["category"], "manual_note")
                updated = update_event(conn, manual["id"], timestamp_ns=6, reason="other", description="Updated")
                self.assertEqual(updated["reason"], "other")
                self.assertEqual(updated["category"], "other")
                self.assertEqual(timeline["events"][0]["category"], "brake_takeover")
                self.assertEqual(len(get_file_events(conn, file_id)), 2)
                self.assertTrue(delete_or_clear_event(conn, manual["id"])["deleted"])

    def test_ignore_takeover_persistence_statistics_and_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "minute" / "gaea" / "a.record"
            source.parent.mkdir(parents=True); source.write_bytes(b"x")
            db = root / "test.sqlite3"
            with connect(db) as conn:
                init_db(conn)
                run_id = create_run(conn, "run", "v1", str(root), [(source, output_for(source))])
                file_id = conn.execute("select id from run_files").fetchone()[0]
                conn.execute("update run_files set begin_time_ns=0,end_time_ns=10000000000")
                analyzer = RunAnalyzer(); analyzer.start_ns = 0; analyzer.end_ns = 10_000_000_000
                analyzer.state_segments = [{"start_time_ns": 0, "end_time_ns": 10_000_000_000,
                                            "state": "active", "reason": None, "evidence": {}}]
                analyzer.events = [{"timestamp_ns": t, "event_type": "closed_loop_exit", "reason": reason,
                                    "auto_description": reason, "diagnostic": {}}
                                   for t, reason in [(1_000_000_000, "brake_takeover"),
                                                     (1_800_000_000, "brake_takeover"),
                                                     (3_000_000_000, "steering_takeover")]]
                save_analysis(conn, run_id, analyzer)
                events = get_file_events(conn, file_id)
                target = events[1]["id"]
                with self.assertRaisesRegex(ValueError, "required"):
                    update_event(conn, target, ignored=True, ignore_reason="  ")
                update_event(conn, target, ignored=True, ignore_reason="False positive", description="Reviewed")
                update_event(conn, events[2]["id"], ignored=True, ignore_reason="Test maneuver")
                summary = get_timeline(conn, run_id)["summary"]
                self.assertEqual((summary["brake_takeovers"], summary["brake_takeovers_ignored"], summary["brake_takeovers_total"]), (1, 1, 2))
                self.assertEqual((summary["steering_takeovers"], summary["steering_takeovers_ignored"]), (0, 1))
                self.assertEqual(summary["closed_loop_duration_s"], 10)
                update_event(conn, target, reason="other")
                self.assertEqual(get_timeline(conn, run_id)["summary"]["other_exits"], 0)
                delete_or_clear_event(conn, target)
                self.assertTrue(get_file_events(conn, file_id)[1]["ignored"])
                manual = create_manual_event(conn, file_id, 5, "brake_takeover", "Manual note")
                with self.assertRaisesRegex(ValueError, "only automatic"):
                    update_event(conn, manual["id"], ignored=True, ignore_reason="No")
                # The closest previous event is the second, not the first within one second.
                analyzer.events = [dict(analyzer.events[1], timestamp_ns=1_700_000_000)]
                save_analysis(conn, run_id, analyzer)
                matched = [e for e in get_file_events(conn, file_id) if e["source"] == "auto"][0]
                self.assertTrue(matched["ignored"])
                self.assertEqual(matched["ignore_reason"], "False positive")
            with connect(db) as conn:
                init_db(conn)
                restored = update_event(conn, matched["id"], ignored=False)
                self.assertFalse(restored["ignored"])
                self.assertEqual(restored["ignore_reason"], "False positive")
                self.assertEqual(get_timeline(conn, run_id)["summary"]["brake_takeovers"], 1)

    def test_output_location_and_api_path_parsing(self):
        source = Path("/data/20260917191600/gaea/car.record")
        self.assertEqual(output_for(source), Path("/data/20260917191600/mcap/car.mcap"))
        self.assertEqual(parse_run_path("/api/runs/12/series"), (12, "series"))
        self.assertEqual(parse_file_open_path("/api/run-files/9/open"), 9)
        self.assertEqual(parse_run_file_path("/api/run-files/9/events"), (9, "events"))
        self.assertEqual(parse_event_path("/api/events/7"), 7)


if __name__ == "__main__":
    unittest.main()
