"""Timestamp-ordered run analysis for PerfBoard timelines and events."""
from __future__ import annotations

import copy
import math

from .cyber import RecordReader, descriptor_set

POSE_TOPIC = "/hirain/localization/global_pose"
CONTROL_TOPIC = "/hirain/control/control_signal"
VEHICLE_TOPIC = "/hirain/eth/Vehicle_Info_Any"
STATE_TOPIC = "/hirain/state/state_engine_output"
ANALYSIS_TOPICS = {POSE_TOPIC, CONTROL_TOPIC, VEHICLE_TOPIC, STATE_TOPIC}
GPS_INTERVAL_NS = 200_000_000
FEEDBACK_MAX_AGE_NS = 200_000_000
TRANSIENT_GAP_NS = 200_000_000
CAUSE_WINDOW_NS = 300_000_000
MAX_PLAUSIBLE_SPEED_MPS = 100.0
EARTH_RADIUS_M = 6_371_008.8
GCJ_A = 6378245.0
GCJ_EE = 0.006693421622965943
CLOSED_STATES = {"active", "longitudinal_override"}

EVENT_DESCRIPTIONS = {
    "brake_takeover": "Brake takeover",
    "steering_takeover": "Steering takeover",
    "eps_abort": "EPS control aborted",
    "closed_loop_disabled": "Closed-loop disabled",
    "feedback_lost": "Vehicle feedback unavailable",
    "other": "Closed-loop exited",
    "longitudinal_override": "Longitudinal override",
}


class RunAnalyzer:
    topics = ANALYSIS_TOPICS

    def __init__(self):
        self.gps_points = []
        self.closed_loop_events = []
        self.state_segments = []
        self.events = []
        self.distance_m = 0.0
        self.start_ns = None
        self.end_ns = None
        self.warnings = []
        self._last_valid_gps = None
        self._last_saved_ns = None
        self._pending_gps = None
        self._vehicle_ns = None
        self._control_ns = None
        self._state_ns = None
        self._eps_status = self._driver_override = self._brake_status = 0
        self._eps_abort_reason = 0
        self._control_enabled = self._lat_override = self._lon_override = False
        self._noa_state = 0
        self._reason_codes = []
        self._control_seen = self._vehicle_seen = self._state_seen = False
        self._samples = []

    def checkpoint(self):
        return copy.deepcopy(self.__dict__)

    def restore(self, checkpoint):
        self.__dict__ = checkpoint

    def observe_range(self, begin_ns, end_ns):
        self.start_ns = begin_ns if self.start_ns is None else min(self.start_ns, begin_ns)
        self.end_ns = end_ns if self.end_ns is None else max(self.end_ns, end_ns)

    def process(self, topic, message, ns):
        self.start_ns = ns if self.start_ns is None else min(self.start_ns, ns)
        self.end_ns = ns if self.end_ns is None else max(self.end_ns, ns)
        if topic == POSE_TOPIC:
            self._pose(message, ns)
        elif topic == VEHICLE_TOPIC:
            self._vehicle_seen = True
            self._vehicle_ns = ns
            self._eps_status = int(getattr(message, "eps_lccontrolstatus", 0))
            self._driver_override = int(getattr(message, "vcu_indicateddriveroverride", 0))
            self._brake_status = int(getattr(message, "vcu_brkpedalstasus", 0))
            self._eps_abort_reason = int(getattr(message, "eps_abortreason", 0))
            self._sample_system_state(ns)
        elif topic == CONTROL_TOPIC:
            self._control_seen = True
            self._control_ns = ns
            self._control_enabled = bool(getattr(message, "control_noa_enable", False))
            self._lat_override = bool(getattr(message, "lat_override", False))
            self._lon_override = bool(getattr(message, "lon_override", False))
            self._sample_system_state(ns)
        elif topic == STATE_TOPIC:
            self._state_seen = True
            if self._state_ns is not None and ns - self._state_ns > FEEDBACK_MAX_AGE_NS:
                self._record_sample(self._state_ns + FEEDBACK_MAX_AGE_NS, "unknown", "feedback_lost", {})
            self._state_ns = ns
            self._noa_state = int(getattr(message, "NOA_State", 0))
            self._reason_codes = list(getattr(message, "reason_code", []))
            self._sample_system_state(ns)

    def _record_sample(self, ns, state, reason, evidence):
        if not self._samples or self._samples[-1][1] != state or self._samples[-1][2] != reason:
            self._samples.append((ns, state, reason, evidence))

    def _sample_system_state(self, ns):
        state, reason, evidence = self._classify(ns)
        self._record_sample(ns, state, reason, evidence)

    def _classify(self, ns):
        control_fresh = self._control_ns is not None and 0 <= ns - self._control_ns <= FEEDBACK_MAX_AGE_NS
        vehicle_fresh = self._vehicle_ns is not None and 0 <= ns - self._vehicle_ns <= FEEDBACK_MAX_AGE_NS
        state_fresh = self._state_ns is not None and 0 <= ns - self._state_ns <= FEEDBACK_MAX_AGE_NS
        evidence = {
            "NOA_State": self._noa_state,
            "reason_code": self._reason_codes,
            "control_noa_enable": self._control_enabled,
            "lat_override": self._lat_override,
            "lon_override": self._lon_override,
            "eps_lccontrolstatus": self._eps_status,
            "vcu_indicateddriveroverride": self._driver_override,
            "vcu_brkpedalstasus": self._brake_status,
            "eps_abortreason": self._eps_abort_reason,
        }
        if not state_fresh:
            return "unknown", "feedback_lost", evidence
        if self._noa_state == 4:
            longitudinal_override = ((control_fresh and self._lon_override) or
                                     (vehicle_fresh and self._driver_override != 0))
            return ("longitudinal_override", None, evidence) if longitudinal_override else ("active", None, evidence)
        if vehicle_fresh and self._brake_status != 0:
            return "inactive", "brake_takeover", evidence
        if (control_fresh and self._lat_override) or (vehicle_fresh and self._eps_abort_reason == 2):
            return "inactive", "steering_takeover", evidence
        if control_fresh and not self._control_enabled:
            return "inactive", "closed_loop_disabled", evidence
        if vehicle_fresh and self._eps_status != 1:
            return "inactive", "eps_abort", evidence
        return "inactive", "other", evidence

    def _pose(self, message, ns):
        if not message.HasField("position_llh"):
            return
        p = message.position_llh
        if not all(math.isfinite(v) for v in (p.lon, p.lat, p.height)) or not (-180 <= p.lon <= 180 and -90 <= p.lat <= 90):
            return
        lon, lat = gcj02_to_wgs84(p.lon, p.lat)
        current = (ns, lon, lat, p.height)
        if self._last_valid_gps is not None:
            dt = (ns - self._last_valid_gps[0]) / 1e9
            distance = haversine(self._last_valid_gps[1], self._last_valid_gps[2], lon, lat)
            if dt <= 0:
                return
            if distance / dt > MAX_PLAUSIBLE_SPEED_MPS:
                if "Ignored implausible GPS jumps" not in self.warnings:
                    self.warnings.append("Ignored implausible GPS jumps")
                self._last_valid_gps = current
                return
            self.distance_m += distance
        self._last_valid_gps = current
        self._pending_gps = current
        if self._last_saved_ns is None or ns - self._last_saved_ns >= GPS_INTERVAL_NS:
            self.gps_points.append(current)
            self._last_saved_ns = ns

    def finalize(self):
        if self._pending_gps is not None and (not self.gps_points or self.gps_points[-1][0] != self._pending_gps[0]):
            self.gps_points.append(self._pending_gps)
        if self.start_ns is None or self.end_ns is None or not self._state_seen:
            return
        if self._state_ns is not None and self.end_ns - self._state_ns > FEEDBACK_MAX_AGE_NS:
            self._record_sample(self._state_ns + FEEDBACK_MAX_AGE_NS, "unknown", "feedback_lost", {})
        raw = self._segments_from_samples()
        self.state_segments = self._merge_transient_gaps(raw)
        self.closed_loop_events = self._binary_events(self.state_segments)
        self.events = self._derive_events(self.state_segments)

    def _segments_from_samples(self):
        samples = list(self._samples)
        if not samples:
            return [{"start_time_ns": self.start_ns, "end_time_ns": self.end_ns,
                     "state": "unknown", "reason": "feedback_lost", "evidence": {}}]
        segments = []
        if samples[0][0] > self.start_ns:
            segments.append({"start_time_ns": self.start_ns, "end_time_ns": samples[0][0],
                             "state": "unknown", "reason": "feedback_lost", "evidence": {}})
        for index, (timestamp, state, reason, evidence) in enumerate(samples):
            end = samples[index + 1][0] if index + 1 < len(samples) else self.end_ns
            if end > timestamp:
                segments.append({"start_time_ns": timestamp, "end_time_ns": end,
                                 "state": state, "reason": reason, "evidence": evidence})
        return self._coalesce(segments)

    def _merge_transient_gaps(self, segments):
        segments = [dict(item) for item in segments]
        changed = True
        while changed:
            changed = False
            if len(segments) > 1 and segments[0]["state"] == "unknown" and segments[0]["end_time_ns"] - segments[0]["start_time_ns"] < TRANSIENT_GAP_NS:
                segments[1]["start_time_ns"] = segments[0]["start_time_ns"]
                segments.pop(0); changed = True; continue
            if len(segments) > 1 and segments[-1]["state"] == "unknown" and segments[-1]["end_time_ns"] - segments[-1]["start_time_ns"] < TRANSIENT_GAP_NS:
                segments[-2]["end_time_ns"] = segments[-1]["end_time_ns"]
                segments.pop(); changed = True; continue
            for index in range(1, len(segments) - 1):
                current, before, after = segments[index], segments[index - 1], segments[index + 1]
                duration = current["end_time_ns"] - current["start_time_ns"]
                transient = current["state"] == "unknown" or current["reason"] in (None, "feedback_lost", "other")
                if duration < TRANSIENT_GAP_NS and transient and before["state"] == after["state"]:
                    before["end_time_ns"] = after["end_time_ns"]
                    segments[index - 1:index + 2] = [before]
                    changed = True
                    break
        return self._coalesce(segments)

    @staticmethod
    def _coalesce(segments):
        result = []
        for segment in segments:
            if segment["end_time_ns"] <= segment["start_time_ns"]:
                continue
            if result and result[-1]["state"] == segment["state"] and result[-1].get("reason") == segment.get("reason"):
                result[-1]["end_time_ns"] = segment["end_time_ns"]
            else:
                result.append(dict(segment))
        return result

    @staticmethod
    def _binary_events(segments):
        result = []
        previous = None
        for segment in segments:
            value = 1.0 if segment["state"] in CLOSED_STATES else 0.0
            if value != previous:
                result.append((segment["start_time_ns"], value))
                previous = value
        if result and result[-1][1] > .5:
            result.append((segments[-1]["end_time_ns"], 0.0))
        return result

    @staticmethod
    def _derive_events(segments):
        events = []
        priority = {"brake_takeover": 0, "steering_takeover": 1, "eps_abort": 2,
                    "closed_loop_disabled": 3, "feedback_lost": 4, "other": 5, None: 6}
        for index, (before, after) in enumerate(zip(segments, segments[1:])):
            timestamp = after["start_time_ns"]
            if before["state"] != "longitudinal_override" and after["state"] == "longitudinal_override":
                events.append(_event(timestamp, "longitudinal_override", "longitudinal_override", after))
            if before["state"] in CLOSED_STATES and after["state"] not in CLOSED_STATES:
                candidates = [after]
                for later in segments[index + 2:]:
                    if later["start_time_ns"] - timestamp > CAUSE_WINDOW_NS:
                        break
                    if later["state"] not in CLOSED_STATES:
                        candidates.append(later)
                cause = min(candidates, key=lambda item: priority.get(item.get("reason"), 5))
                events.append(_event(timestamp, "closed_loop_exit", cause.get("reason") or "other", cause))
        return events


def _event(timestamp, event_type, reason, segment):
    return {
        "timestamp_ns": timestamp,
        "end_time_ns": None,
        "event_type": event_type,
        "reason": reason,
        "auto_description": EVENT_DESCRIPTIONS.get(reason, "Closed-loop event"),
        "diagnostic": segment.get("evidence") or {},
    }


def scan_record(path, analyzer):
    """Decode only analysis topics, then process them in timestamp order."""
    reader = RecordReader(path)
    channels = reader.channels()
    decoders = {topic: descriptor_set(channels[topic])[1] for topic in analyzer.topics if topic in channels}
    ordered = []
    for sequence, item in enumerate(reader.messages()):
        cls = decoders.get(item.channel_name)
        if cls is not None:
            ordered.append((item.time, sequence, item.channel_name, cls.FromString(item.content)))
    ordered.sort(key=lambda item: (item[0], item[1]))
    for ns, _, topic, message in ordered:
        analyzer.process(topic, message, ns)


def haversine(lon1, lat1, lon2, lat2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return EARTH_RADIUS_M * 2 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1 - a)))


def gcj02_to_wgs84(lon, lat):
    if lon < 72.004 or lon > 137.8347 or lat < 0.8293 or lat > 55.8271:
        return lon, lat
    wlon, wlat = lon, lat
    for _ in range(3):
        glon, glat = wgs84_to_gcj02(wlon, wlat)
        wlon -= glon - lon
        wlat -= glat - lat
    return wlon, wlat


def wgs84_to_gcj02(lon, lat):
    x, y = lon - 105.0, lat - 35.0
    dlat = -100 + 2*x + 3*y + .2*y*y + .1*x*y + .2*math.sqrt(abs(x))
    dlon = 300 + x + 2*y + .1*x*x + .1*x*y + .1*math.sqrt(abs(x))
    dlat += (20*math.sin(6*x*math.pi)+20*math.sin(2*x*math.pi))*2/3
    dlat += (20*math.sin(y*math.pi)+40*math.sin(y/3*math.pi))*2/3
    dlat += (160*math.sin(y/12*math.pi)+320*math.sin(y*math.pi/30))*2/3
    dlon += (20*math.sin(6*x*math.pi)+20*math.sin(2*x*math.pi))*2/3
    dlon += (20*math.sin(x*math.pi)+40*math.sin(x/3*math.pi))*2/3
    dlon += (150*math.sin(x/12*math.pi)+300*math.sin(x/30*math.pi))*2/3
    rad = math.radians(lat); magic = 1-GCJ_EE*math.sin(rad)**2; root = math.sqrt(magic)
    dlat = dlat*180/((GCJ_A*(1-GCJ_EE)/(magic*root))*math.pi)
    dlon = dlon*180/(GCJ_A/root*math.cos(rad)*math.pi)
    return lon+dlon, lat+dlat
