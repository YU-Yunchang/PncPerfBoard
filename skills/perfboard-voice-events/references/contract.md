# Voice contract v1

Run the helper from the root of your local PerfBoard checkout, using its `.venv` Python.

Input: `schema_version: 1`, `kind: "perfboard_transcript"`, `recording_id`, `sha256`, `start_time_ns` (decimal string), `duration_ms`, and `segments` with unique `id`, `text`, integer `start_ms`, `end_ms`, `absolute_start` and ASR indicators. Recording metadata supplies the absolute time base; no spoken time is required.

Output:

```json
{
  "schema_version": 1,
  "kind": "perfboard_voice_events",
  "recording_id": "COPY_FROM_TRANSCRIPT",
  "sha256": "COPY_FROM_TRANSCRIPT",
  "events": [{
    "id": "v001",
    "category": "issue",
    "title": "公交车强行汇入时未及时制动",
    "description": "对强行汇入的公交车制动响应不足，驾驶员刹车接管。",
    "reason": "brake_takeover",
    "takeover": "brake",
    "segment_ids": ["s0000", "s0001"]
  }]
}
```

IDs must be unique, nonempty and stable across edits. Category: `issue`, `check`, `positive`. Takeover: `none`, `brake`, `steering`, `unspecified`. Reason: normally `other`, `brake_takeover`, `steering_takeover`, or `manual_note`; supported existing reasons also include `eps_abort`, `closed_loop_disabled`, `feedback_lost`, `longitudinal_override`, `longitudinal_override_started`, `longitudinal_override_ended`.

## Classification and display

The import fields describe independent dimensions:

- Import `category` is the transcript assessment, preserved as nested `voice.category` after import and displayed as the large **Issue**, **Check**, or **Positive** heading. Use `issue` for reported unwanted behavior, `check` for a request to verify or investigate, and `positive` for explicitly satisfactory or expected behavior. Follow the final spoken assessment, including corrections; uncertainty about a technical term alone does not change this assessment. It is distinct from the grey event-type Category metadata.
- `reason` is auxiliary technical or legacy event-type information. Choose an evidence-supported existing reason; use `other` when no more specific reason applies. `manual_note` remains supported for compatibility. Do not map `other` to `issue` or `manual_note` to `check`: an `other` event can be positive, and a `manual_note` can have any transcript assessment. Do not put display labels into this field.
- `takeover` describes an explicitly completed takeover and controls automatic exit association. It is independent of category and reason: a check can include an unspecified completed takeover, and an issue can have no takeover.

PerfBoard's top-level timeline/API `event.category` is the event type shown in grey card metadata under **Category**, replacing the former grey Reason text. Audio evidence and the event editor also label this event type **Category**; it does not replace the large voice assessment heading:

- Automatic and manually entered events use their effective event type, including any user correction.
- A voice event linked to an existing automatic event inherits that automatic event's category, rather than its own imported `reason` or transcript assessment.
- Every other voice event uses `manual_note`, displayed as **Manual Note**. This includes unmatched, ambiguous, type-conflict, explicitly unlinked/auto-match-disabled reports, and links whose target no longer exists, even if the voice event's imported `reason` was edited.

A merged automatic/voice card keeps its automatic takeover/exit heading and the voice **Issue**, **Check**, or **Positive** assessment on two lines with identical heading typography, not a small trailing badge. Its grey Category metadata uses the shared automatic event type. Keep **Edit**, **Ignore** (or **Restore**, when eligible), and **Review audio** as independent actions. **Ignore reason** still means the explanation for excluding an event from statistics.

Do not rewrite import `category`, `reason`, or `takeover` to achieve this display behavior. The import schema and enums are unchanged; existing imports derive the current event category on read and do not need regeneration or reimporting. Transcript assessments remain available as evidence metadata.

## Finalize and import

Run from the project directory:

```sh
.venv/bin/python -m record2mcap.voice_tools finalize --transcript /absolute/transcript.json --events /absolute/draft.json --output /absolute/events.json
```

The helper validates evidence references and identity, derives the clip start/end milliseconds from the referenced segments, and adds a decimal-string `timestamp_ns` using integer arithmetic. It rejects supplied timestamps that differ from evidence. Do not manually estimate nanoseconds. PerfBoard imports the finalized file under the corresponding recording. Imported event time can later be edited independently of immutable audio evidence time.

Automatic takeover linking searches the same Run for an automatic closed-loop exit from 30 seconds before to 5 seconds after the voice event time. Explicit takeover types prefer identical detected reasons; ambiguous candidates require human review. The voice report and exit remain separate records and count as only one automatic exit in statistics.
