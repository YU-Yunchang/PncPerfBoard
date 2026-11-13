# Road-test voice events (optional)

The normal launcher does not install Whisper or download models. Run import, MCAP
conversion, maps, closed-loop analysis and manual events work without it. The UI
shows an optional-feature notice and disables audio upload/transcription when the
local tools are absent.

The current worker uses MLX and therefore targets Apple Silicon Macs. Windows,
Linux and Intel Macs should skip this extension for now. On Apple Silicon, create
a separate environment with `python3 -m venv .venv-transcribe`, then install
`mlx-whisper` and `imageio-ffmpeg` using that environment's pip. Cache
`mlx-community/whisper-large-v3-turbo` in that same environment before transcribing;
the worker explicitly runs offline and cannot download a missing model. Configure
`PERFBOARD_WHISPER_PYTHON` if the environment is elsewhere, and
`PERFBOARD_FFMPEG` if FFmpeg is not on PATH or bundled by imageio-ffmpeg. Restart
PerfBoard after configuring these tools. This extension is not part of the basic
installation or cross-platform smoke test.


In Run Details, upload M4A/MP3/WAV under **Road-test Recordings**. Confirm the start time (ISO 8601 with timezone) and click **Transcribe**. Download the transcript JSON and give it to Codex with `$perfboard-voice-events`. Import its returned event JSON under the same recording. Review audio from the event list.

Whisper runs offline in a separate worker using the existing `.venv-transcribe` environment and cached `mlx-community/whisper-large-v3-turbo`. Optional overrides: `PERFBOARD_WHISPER_PYTHON` and `PERFBOARD_FFMPEG`. The normal PerfBoard Python environment does not import MLX. Missing dependencies/model/GPU errors appear as failed tasks, which can be retried. No language-model API is called.

Original audio, browser playback audio, raw Whisper result and normalized transcript are kept next to the database under `voice/<recording-id>/`. Do not move these files independently of the database. Start time becomes immutable after importing events. Correct event time in the event editor without changing evidence playback time. Continuous, unedited audio is assumed.

The JSON contract and extraction instructions are in `skills/perfboard-voice-events/references/contract.md`. Use `python -m record2mcap.voice_tools finalize` to validate and calculate timestamps. The `normalize` command can wrap an existing Whisper JSON for debugging; recording identity must match the target upload.

Voice event cards keep **Issue**, **Check**, or **Positive** as their large assessment heading. The grey event-type metadata is labeled **Category**, replacing the former Reason text. The timeline/API's top-level `event.category` is derived on read: automatic and manually entered events use their effective event type; voice reports linked to an existing automatic event inherit that event's category; all remaining voice events use `manual_note`, displayed as **Manual Note**, regardless of their imported or edited `reason`. Remaining reports include unmatched, ambiguous, type-conflict, auto-match-disabled/unlinked reports, and missing link targets. A merged card keeps the automatic takeover/exit heading and the voice assessment on two lines with identical heading typography, plus separate **Edit**, **Ignore** (or **Restore**, when eligible), and **Review audio** actions. Category also labels the event type in audio evidence and the event editor; the explanatory **Ignore reason** field is unchanged.

The import JSON's `category` and nested `voice.category` preserve the transcript assessment (**Issue**, **Check**, or **Positive**) used for the large heading. Do not rewrite them to `manual_note` or a matched exit type. Import `reason` and reported `takeover` also remain unchanged; takeover controls linking, not the transcript assessment or grey category. A Check can describe an explicitly completed takeover. The schema and enums are unchanged, and existing imports acquire the derived grey category without regeneration or reimporting.

Reported takeovers search automatic closed-loop exits in the same Run from 30 seconds before to 5 seconds after the report. Explicit brake/steering reports match the same original detected reason. With multiple candidates, the nearest must beat the next by at least 5 seconds; otherwise review manually. Normal/negated takeovers are not linked. Ignore flags and annotations remain intact. Linked voice events are merged visually under the automatic exit, not counted again. **Unlink** disables automatic relinking; **Match automatically** restores it.

HTTP interfaces:

- `POST /api/runs/{id}/recordings`: raw audio body, URL-encoded `X-Filename`; returns recording identity and metadata.
- `GET /api/runs/{id}/recordings`: task list.
- `POST /api/recordings/{id}/transcribe`: optional `start_time` override; enqueue/retry.
- `GET /api/recordings/{id}/status`, `/transcript`, `/audio`: status, contract download, seekable audio.
- `POST /api/recordings/{id}/events`: validated event JSON; returns added/skipped counts. Duplicate external IDs preserve user edits.
- `POST /api/events/{id}/link`: `{mode: "manual", target_id: 123}`, `{mode: "disabled"}`, or `{mode: "auto"}`.

