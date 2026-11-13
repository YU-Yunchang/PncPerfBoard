// Zero-dependency rendering regressions; run with `node --test tests/test_perfboard_frontend.cjs`.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const staticDirectory = path.join(__dirname, "..", "record2mcap", "perfboard_static");

function frontend() {
  const elements = new Map();
  function element(id) {
    if (!elements.has(id)) {
      elements.set(id, {
        id,
        hidden: id === "voice-modal",
        innerHTML: "",
        textContent: "",
        value: "",
        disabled: false,
        checked: false,
        dataset: {},
        clientWidth: 600,
        attributes: {},
        setAttribute(name, value) { this.attributes[name] = value; },
        removeAttribute(name) { delete this.attributes[name]; },
        querySelectorAll() { return []; },
        focus() { this.focused = true; },
      });
    }
    return elements.get(id);
  }
  const context = vm.createContext({
    document: {
      // Loading the scripts must not start the app or issue requests.
      addEventListener() {},
      getElementById: element,
      activeElement: { isConnected: false },
    },
  });
  for (const filename of ["app.js", "voice.js"]) {
    vm.runInContext(fs.readFileSync(path.join(staticDirectory, filename), "utf8"), context, { filename });
  }
  return {
    element,
    run(source, values = {}) {
      Object.assign(context, values);
      return vm.runInContext(source, context);
    },
  };
}

function voiceEvent(category = "issue", reason = "other", additions = {}) {
  return {
    id: 41,
    run_id: 1,
    source: "voice",
    event_type: "voice_report",
    timestamp_ns: "1790486951000000000",
    reason,
    auto_description: "Reported detail",
    voice: {
      category,
      title: "Report title",
      takeover: "none",
      match_status: "not_applicable",
      link_mode: "auto",
      linked_event_id: null,
      start_ms: 0,
      end_ms: 1000,
      duration_ms: 3000,
      absolute_start: "2026-09-27T05:29:11Z",
      absolute_end: "2026-09-27T05:29:12Z",
      audio_url: "/api/recordings/example/audio",
      transcript: "Original speech",
    },
    ...additions,
  };
}

function automaticEvent(reason = "brake_takeover", additions = {}) {
  return {
    id: 12,
    run_id: 1,
    source: "auto",
    event_type: "closed_loop_exit",
    timestamp_ns: "1790486951000000000",
    reason,
    detected_reason: reason,
    ...additions,
  };
}

for (const assessment of ["issue", "check", "positive"]) {
  for (const reason of ["other", "manual_note", "brake_takeover", "steering_takeover"]) {
    test(`Unlinked ${assessment} keeps its heading and uses grey Manual Note Category despite extracted ${reason}`, () => {
      const app = frontend();
      const event = voiceEvent(assessment, reason);
      event.voice.takeover = reason === "brake_takeover" ? "brake" : reason === "steering_takeover" ? "steering" : "none";
      const before = JSON.stringify(event);
      assert.equal(app.run("eventCategory(event)", { event }), "manual_note");
      const heading = { issue: "Issue", check: "Check", positive: "Positive" }[assessment];
      assert.equal(app.run("eventLabel(event)", { event }), heading);
      const markup = app.run("eventCardMarkup(event)", { event });
      assert.match(markup, new RegExp(`<b data-category="manual_note">${heading}</b>`));
      assert.match(markup, /class="event-meta">voice · Category: Manual Note/);
      assert.doesNotMatch(markup, /Reason:|event-voice-heading|data-category="(?:issue|check|positive)"/);
      assert.match(markup, /data-edit-event="41"/);
      assert.match(markup, /data-review-audio="41"/);
      assert.doesNotMatch(markup, /data-ignore-event=/);
      assert.equal(JSON.stringify(event), before, "Displaying an event category must preserve the original reason and assessment");
    });
  }
}

test("Automatic takeovers and linked assessments retain two matching heading lines and all three actions", () => {
  const app = frontend();
  for (const [reason, label] of [["brake_takeover", "Brake Takeover"], ["steering_takeover", "Steering Takeover"]]) {
    const event = automaticEvent(reason, {
      voice_reports: [voiceEvent("issue"), voiceEvent("issue"), voiceEvent("check"), voiceEvent("positive")],
    });
    const markup = app.run("eventCardMarkup(event)", { event });
    assert.match(markup, new RegExp(`<b data-category="${reason}">${label}</b>`));
    assert.equal((markup.match(/class="event-heading(?: event-voice-heading)?"/g) || []).length, 2);
    assert.equal((markup.match(/data-category=/g) || []).length, 1);
    assert.match(markup, /event-voice-heading"><b data-assessment="issue">Issue<\/b> · <b data-assessment="check">Check<\/b> · <b data-assessment="positive">Positive<\/b><\/div>/);
    assert.equal((markup.match(/data-assessment="issue"/g) || []).length, 1);
    assert.doesNotMatch(markup, /class="voice-category"|data-category="(?:issue|check|positive)"/);
    assert.match(markup, new RegExp(`Category: ${label}`));
    for (const action of ["edit-event", "ignore-event", "review-audio"]) {
      assert.match(markup, new RegExp(`data-${action}="12"`));
    }
    assert.match(markup, /4 voice reports/);
  }
});

test("Manual events and automatic Other events retain their category headings", () => {
  const app = frontend();
  const manual = { id: 14, source: "manual", reason: "manual_note" };
  assert.equal(app.run("eventLabel(event)", { event: manual }), "Manual Note");
  assert.equal(app.run("eventLabel(event)", { event: automaticEvent("other") }), "Other");
  const corrected = automaticEvent("brake_takeover", { category: "steering_takeover" });
  assert.equal(app.run("eventLabel(event)", { event: corrected }), "Steering Takeover");
});

test("Ignore remains unavailable to direct voice events and Restore stays on ignored takeovers", () => {
  const app = frontend();
  const voice = voiceEvent("issue", "brake_takeover");
  assert.equal(app.run("canIgnoreEvent(event)", { event: voice }), false);
  const ignored = automaticEvent("brake_takeover", { ignored: true, ignore_reason: "Test override" });
  assert.match(app.run("eventActions(event)", { event: ignored }), /data-ignore-event="12">Restore/);
});

test("Original assessment remains the heading but does not control takeover matching or grey Category", () => {
  const app = frontend();
  const event = voiceEvent("check", "manual_note");
  event.voice.takeover = "unspecified";
  event.voice.match_status = "ambiguous";
  assert.equal(app.run("eventLabel(event)", { event }), "Check");
  assert.equal(app.run("eventCategory(event)", { event }), "manual_note");
  assert.match(app.run("eventMetadata(event)", { event }), /Needs review/);
  event.voice.match_status = "linked";
  event.voice.linked_event_id = 12;
  assert.match(app.run("eventMetadata(event)", { event }), /Linked/);
  event.voice.link_mode = "disabled";
  assert.match(app.run("eventMetadata(event)", { event }), /Auto-match disabled/);
});

for (const [category, label] of [["brake_takeover", "Brake Takeover"], ["steering_takeover", "Steering Takeover"], ["other", "Other"], ["manual_note", "Manual Note"]]) {
  test(`Linked voice events inherit automatic ${label}, including corrected categories`, () => {
    const app = frontend();
    const linked = voiceEvent("positive", "other");
    linked.voice.linked_event_id = 12;
    linked.voice.match_status = "linked";
    const parent = automaticEvent("brake_takeover", { category, effective_reason: category, voice_reports: [linked] });
    const before = JSON.stringify(linked);
    app.run("state.timeline={events}", { events: [parent, linked] });
    assert.equal(app.run("eventCategory(event)", { event: linked }), category);
    assert.equal(app.run("eventLabel(event)", { event: linked }), "Positive");
    assert.match(app.run("eventMetadata(event)", { event: linked }), new RegExp(`Category: ${label}`));
    assert.equal(JSON.stringify(linked), before);
    // Modern API payloads also expose the inherited event category.
    linked.category = category;
    assert.equal(app.run("eventLabel(event)", { event: linked }), "Positive");
    linked.voice.linked_event_id = null;
    linked.voice.match_status = "unmatched";
    assert.equal(app.run("eventLabel(event)", { event: linked }), "Positive", "Unlinking must not change the transcript assessment heading");
    assert.equal(app.run("eventCategory(event)", { event: linked }), "manual_note", "Unlinking must override a previously inherited grey category");
  });
}

test("Valid linked reports are hidden as duplicate cards but retained on their automatic parent", () => {
  const app = frontend();
  const linked = voiceEvent();
  linked.voice.linked_event_id = 12;
  linked.voice.match_status = "linked";
  const standalone = voiceEvent("positive", "other", { id: 42 });
  const parent = automaticEvent("brake_takeover", { voice_reports: [linked] });
  const ids = app.run("state.timeline={events};visibleEvents().map(event=>event.id)", { events: [parent, linked, standalone] });
  assert.deepEqual(Array.from(ids), [12, 42]);
  assert.match(app.run("eventCardMarkup(event)", { event: parent }), /data-category="brake_takeover"/);
});

for (const [name, targetChanges, voiceChanges] of [
  ["missing target", null, {}],
  ["manual target", { source: "manual" }, {}],
  ["non-exit target", { event_type: "longitudinal_override" }, {}],
  ["different Run", { run_id: 2 }, {}],
  ["disabled association", {}, { link_mode: "disabled" }],
  ["ambiguous status", {}, { match_status: "ambiguous" }],
  ["type conflict", {}, { match_status: "type_conflict" }],
  ["unmatched status", {}, { match_status: "unmatched" }],
  ["no reported takeover", {}, { match_status: "not_applicable" }],
]) {
  test(`Stale ${name} links remain visible as Manual Note`, () => {
    const app = frontend();
    const event = voiceEvent("issue", "brake_takeover", { category: "brake_takeover" });
    Object.assign(event.voice, { linked_event_id: 12, match_status: "linked" }, voiceChanges);
    const target = targetChanges === null ? [] : [automaticEvent("brake_takeover", targetChanges)];
    app.run("state.timeline={events}", { events: [...target, event] });
    assert.equal(app.run("eventCategory(event)", { event }), "manual_note");
    assert.equal(app.run("linkedAutomaticEvent(event)", { event }), null);
    assert.ok(Array.from(app.run("visibleEvents().map(event=>event.id)")).includes(event.id));
  });
}

test("Voice records without evidence still have the fixed Manual Note category", () => {
  const app = frontend();
  const event = { source: "voice", reason: "brake_takeover", category: "issue" };
  assert.equal(app.run("eventCategory(event)", { event }), "manual_note");
  assert.equal(app.run("eventLabel(event)", { event }), "Voice report");
});

test("Unknown voice assessments do not affect category and content remains escaped", () => {
  const app = frontend();
  const event = voiceEvent("unknown", "other", { auto_description: '<img src=x onerror="bad">' });
  assert.equal(app.run("eventLabel(event)", { event }), "Voice report");
  assert.equal(app.run("eventCategory(event)", { event }), "manual_note");
  const markup = app.run("eventCardMarkup(event)", { event });
  assert.match(markup, /&lt;img src=x onerror=&quot;bad&quot;&gt;/);
  assert.doesNotMatch(markup, /<img src=/);
  event.voice.category = 'issue" onclick="bad';
  event.voice.title = '<img src=x onerror="bad">';
  const parent = automaticEvent("brake_takeover", { category: 'other" onclick="bad', voice_reports: [event] });
  const parentMarkup = app.run("eventCardMarkup(event)", { event: parent });
  assert.match(parentMarkup, /data-category="other&quot; onclick=&quot;bad"/);
  assert.match(parentMarkup, /&lt;img src=x onerror=&quot;bad&quot;&gt;/);
  assert.doesNotMatch(parentMarkup, /<img src=|data-category="issue/);
});

test("Voice Evidence separates Manual Note Category from the original Positive assessment", () => {
  const app = frontend();
  const event = voiceEvent("positive", "other");
  app.run("openVoiceEvidence(event)", { event });
  const markup = app.element("voice-evidence").innerHTML;
  assert.match(markup, /voice-field-label">Category<\/span>/);
  assert.match(markup, /voice-readonly">Manual Note<\/div>/);
  assert.match(markup, /voice-report-meta">Category: Manual Note/);
  assert.doesNotMatch(markup, /Reason:|voice-field-label">Reason|class="voice-category"/);
  assert.match(markup, /class="voice-assessment" data-assessment="positive">Positive</);
  assert.match(markup, /data-edit-voice="41"/);
  assert.doesNotMatch(markup, /id="voice-edit-main"/);
});

test("Voice Evidence uses inherited Category throughout linked reports and retains Edit event", () => {
  const app = frontend();
  const report = voiceEvent("check", "manual_note");
  Object.assign(report.voice, { linked_event_id: 12, match_status: "linked" });
  report.link_candidates = [{ id: 12, timestamp_ns: report.timestamp_ns, reason: "brake_takeover", category: "steering_takeover" }];
  const event = automaticEvent("brake_takeover", { category: "steering_takeover", voice_reports: [report] });
  app.run("state.timeline={events}", { events: [event, report] });
  app.run("openVoiceEvidence(event)", { event });
  const markup = app.element("voice-evidence").innerHTML;
  assert.match(markup, /voice-field-label">Category<\/span>/);
  assert.match(markup, /voice-readonly">Steering Takeover<\/div>/);
  assert.match(markup, /id="voice-edit-main"/);
  assert.match(markup, /voice-report-meta">Category: Steering Takeover/);
  assert.match(markup, /<option value="12" selected>[^<]* · Steering Takeover<\/option>/);
  assert.doesNotMatch(markup, /Reason:|voice-field-label">Reason|class="voice-category"/);
  assert.match(markup, /class="voice-assessment" data-assessment="check">Check</);
  app.run("openVoiceEvidence(event)", { event: report });
  assert.match(app.element("voice-evidence").innerHTML, /voice-readonly">Steering Takeover<\/div>/);
});

test("Timeline preserves assessment titles while marker Category follows linking rather than extracted reason", () => {
  const app = frontend();
  const linked = voiceEvent("issue", "brake_takeover", { id: 43 });
  Object.assign(linked.voice, { linked_event_id: 12, match_status: "linked" });
  const events = [voiceEvent("issue", "brake_takeover"), voiceEvent("check", "manual_note", { id: 42 }), automaticEvent("brake_takeover", { category: "steering_takeover", voice_reports: [linked] }), linked];
  app.run("state.run={start_time_ns:'1790486940000000000',end_time_ns:'1790486970000000000'};state.timeline={available:true,segments:[],events};renderSeries()", { events });
  const markup = app.element("series").innerHTML;
  assert.match(markup, /class="event-marker event-manual_note" data-event-id="41"[^>]*><title>[^<]* · Issue ·[^<]*Category: Manual Note/);
  assert.match(markup, /class="event-marker event-manual_note" data-event-id="42"[^>]*><title>[^<]* · Check ·[^<]*Category: Manual Note/);
  assert.match(markup, /class="event-marker event-steering_takeover" data-event-id="12"[^>]*><title>[^<]* · Steering Takeover ·/);
  assert.doesNotMatch(markup, /Reason:|data-event-id="43"/);
  app.run("mapHover=()=>{};updateSeriesHover(Number(event.timestamp_ns),1200,{l:65,r:15},event)", { event: events[0] });
  assert.match(app.element("time-tip").textContent, / · Issue · Reported detail$/);
  app.run("updateSeriesHover(Number(event.timestamp_ns),1200,{l:65,r:15},event)", { event: linked });
  assert.match(app.element("time-tip").textContent, / · Issue · Reported detail$/);
});

test("Voice editors show fixed/inherited read-only Category and preserve their extracted reason", async () => {
  const app = frontend();
  const event = voiceEvent("issue", "other", { run_file_id: 3 });
  const before = JSON.stringify(event);
  app.run("openEventEditor(event,null)", { event });
  assert.equal(app.element("event-reason").value, "manual_note");
  assert.equal(app.element("event-reason").disabled, true);
  assert.equal(app.element("event-category-hint").hidden, false);
  assert.match(app.element("event-category-hint").textContent, /Unlinked voice reports/);
  assert.equal(app.element("event-description").value, "Reported detail");
  assert.equal(JSON.stringify(event), before);

  const requests = [];
  app.run("api=async(url,opt)=>{recordRequest(url,opt);return {}};refreshEventViews=async()=>{}", { recordRequest: (url, opt) => requests.push({ url, method: opt.method, payload: JSON.parse(opt.body) }) });
  await app.run("saveEvent({preventDefault(){}})");
  assert.equal(requests[0].url, "/api/events/41");
  assert.equal(requests[0].method, "PATCH");
  assert.equal(requests[0].payload.description, "Reported detail");
  assert.ok(Number.isFinite(requests[0].payload.timestamp_ns));
  assert.ok(!Object.hasOwn(requests[0].payload, "reason"));
  assert.ok(!Object.hasOwn(requests[0].payload, "category"));

  const parent = automaticEvent("brake_takeover", { category: "steering_takeover" });
  Object.assign(event.voice, { linked_event_id: 12, match_status: "linked" });
  app.run("state.timeline={events};openEventEditor(event,null)", { events: [parent, event], event });
  assert.equal(app.element("event-reason").value, "steering_takeover");
  assert.equal(app.element("event-reason").disabled, true);
  assert.match(app.element("event-category-hint").textContent, /follows the linked automatic exit/);
  await app.run("saveEvent({preventDefault(){}})");
  assert.ok(!Object.hasOwn(requests[1].payload, "reason"));
});

test("Automatic and manual editors keep editable Category and the existing reason API field", async () => {
  const app = frontend();
  const requests = [];
  app.run("api=async(url,opt)=>{recordRequest(url,opt);return {}};refreshEventViews=async()=>{}", { recordRequest: (url, opt) => requests.push({ url, method: opt.method, payload: JSON.parse(opt.body) }) });
  app.run("openEventEditor(event,null)", { event: voiceEvent() });
  const automatic = automaticEvent("brake_takeover", { category: "steering_takeover" });
  app.run("openEventEditor(event,null)", { event: automatic });
  assert.equal(app.element("event-reason").disabled, false, "Opening an automatic event resets the previous voice editor lock");
  assert.equal(app.element("event-reason").value, "steering_takeover");
  assert.equal(app.element("event-category-hint").hidden, true);
  assert.equal(app.element("event-time").disabled, true);
  await app.run("saveEvent({preventDefault(){}})");
  assert.equal(requests[0].payload.reason, "steering_takeover");
  assert.ok(!Object.hasOwn(requests[0].payload, "timestamp_ns"));
  assert.ok(!Object.hasOwn(requests[0].payload, "category"));

  const manual = { id: 14, run_file_id: 3, source: "manual", timestamp_ns: automatic.timestamp_ns, reason: "manual_note" };
  app.run("openEventEditor(event,null)", { event: manual });
  assert.equal(app.element("event-reason").disabled, false);
  assert.equal(app.element("event-time").disabled, false);
  app.element("event-reason").value = "other";
  await app.run("saveEvent({preventDefault(){}})");
  assert.equal(requests[1].payload.reason, "other");
  assert.ok(Number.isFinite(requests[1].payload.timestamp_ns));
  assert.ok(!Object.hasOwn(requests[1].payload, "category"));

  app.run("openEventEditor(null,file)", { file: { id: 3, begin_time_ns: automatic.timestamp_ns } });
  assert.equal(app.element("event-reason").value, "manual_note");
  assert.equal(app.element("event-reason").disabled, false);
  await app.run("saveEvent({preventDefault(){}})");
  assert.equal(requests[2].url, "/api/run-files/3/events");
  assert.equal(requests[2].method, "POST");
  assert.equal(requests[2].payload.reason, "manual_note");
});

test('Missing optional Whisper disables transcription while keeping existing transcript downloads available', () => {
  const ui = frontend();
  const record = {id: 'sample', filename: 'sample.wav', status: 'uploaded', duration_ms: 1000, event_count: 0};
  const card = ui.run('voiceAvailable=false; recordingCard(record)', {record});
  assert.match(card, /data-transcribe="sample" disabled/);
  const ready = ui.run('recordingCard({...record,status:"ready"})');
  assert.match(ready, /Download transcript JSON/);
  assert.match(ready, /Import event JSON/);
});
