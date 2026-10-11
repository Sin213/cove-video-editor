# Local MCP editing and export (M3a)

Install the optional source extra with `pip install '.[mcp]'` in your chosen
environment. It pins `mcp==2.3.0`; the normal GUI neither needs nor imports MCP.
Install FFmpeg/ffprobe separately. Linux source installations are supported;
other platforms fail safely. Packaged builds have not been tested.

Launch using existing, absolute directories:

```sh
python -m cove_video_editor.mcp_server --media-root /absolute/video-directory --output-root /absolute/output-directory
# Equivalent installed entry point: cove-video-editor-mcp
```

Permissions you must set: file operations need `--media-root` and/or
`--output-root` as appropriate; configure both for the complete editing workflow.
The transport is local stdio only. stdout carries protocol messages; diagnostics
go to stderr. No GUI control, network listener, global configuration or defaults
to HOME/CWD are provided. Zero roots starts an unconfigured server. Invalid roots
exit 2; missing optional SDK exits 1. The output root must support hard links.

| Flag | File operations enabled |
|---|---|
| `--media-root DIR` (repeatable) | Inspect/import videos and add audio in DIR and its subfolders |
| `--output-root DIR` | Create project folders, save revisions and publish exports in DIR |

No flags means no file access. The agent cannot change these permissions.

Agent edits live in memory until saved. Existing `.coveproj` editing, scene
understanding and live GUI control remain unavailable. Subtitles, crop and
existing-project import (`open_project`) are deliberately deferred and NOT
available as MCP tools. Background audio and mix controls are supported now.

| Tool | Behavior |
|---|---|
| `cove_get_capabilities` | Permissions, operations and limits; no probes/writes |
| `cove_inspect_media` | Per-path video metadata or bounded error |
| `cove_create_project` | New server-owned project and exclusive directory |
| `cove_import_media` | Atomic import with identity deduplication |
| `cove_set_timeline` | Atomic replacement; sequential or explicit placement |
| `cove_set_audio` | Atomic replacement of ALL added audio; `[]` clears it |
| `cove_set_audio_mix` | Partial mix update; omitted fields retain their values |
| `cove_get_project` | Assets, clips, audio, mix, revision, saved status and warnings |
| `cove_save_project` | Immutable `.coveproj` revision and SHA-256 |
| `cove_start_export` | Start rendering the current saved revision; return job_id |
| `cove_get_export_status` | Poll state, real progress, error and verified output |
| `cove_cancel_export` | Request cancellation; poll until terminal |

Inputs are strict, finite and reject extra fields. Limits: 50 paths/request,
4096 characters/path, 80/name, 20 projects/session, 200 assets/project, 200 clips,
86400 timeline seconds, 50 gap warnings. Probe limits: 10 seconds/file,
60 seconds/request, 65536 output bytes. Speed is 0.25–4; volume is 0–2.
Only `.mp4`, `.mov`, `.mkv`, `.webm`, `.avi`, `.m4v` video files are accepted.
Explicit placement requires `timeline_start_s`; sequential placement forbids it.
Overlaps, invalid trims and missing asset ids are rejected without changing state.

For background audio, use the same startup media roots, including a separate
audio directory via another `--media-root` when needed. Supported extensions:
`.mp3`, `.m4a`, `.aac`, `.wav`, `.ogg`, `.opus`, `.flac`. Extensions alone do not
validate content: the held descriptor is probed, including decoding an initial
one-second window. Audio requires a decodable stream and finite positive
duration of at most 86400 seconds. Cover art is allowed; real video streams in
the audio lane are rejected. `cove_inspect_media` remains video-only.

`cove_set_audio(project_id, items)` replaces the complete added-audio list;
it does not append. Send `[]` to clear it. Each of at most 50 items requires
an absolute authorized `path`. `offset_s` is the start on the timeline;
`src_start_s` and `src_end_s` trim the source, all in seconds. Defaults are
offset/start 0, end at probed duration, lane 1, volume 1.0, mute false.
Lane is an integer 0–63 specifying the audio row. Audio may overlap even on
the same lane; overlapping items are mixed by the existing exporter.
Trims must be nonempty and within the source duration, and offset plus span
must not exceed 86400 seconds. The entire replacement validates before any
state changes. Returned items have server-generated `audio_id`s.

`cove_set_audio_mix` requires at least one of `original_gain`, `added_gain`,
`replace_original`. Omitted fields keep their current values. Gains are linear,
0–3 inclusive, with at most two decimals; finer values are rejected, not rounded.
Each item's volume is 0–2 and is multiplied by added_gain exactly once.
Mute silences an item without changing its stored volume. `replace_original=true`
replaces the clips' own audio with the added audio; false mixes both and applies
original_gain to the original audio. These controls follow the existing exporter's
mixing, including its `amix` normalization; there is no new loudness processing.
Gain 1.0 means no extra gain, rather than the same level as the source: in mix mode original and added audio are each scaled by about 1/2; use `replace_original=true` or gains above 1 to compensate.
Without added audio, these mix controls have no effect on original clip audio.

The video timeline determines export duration: added audio beyond its end is
truncated; offsets at/after that end are inaudible and saving is refused until
they are moved earlier or the timeline is lengthened with `cove_set_timeline`.
`set_audio` accepts these offsets so edits can be made in either order.
`get_project`, `set_timeline`,
`set_audio` and `set_audio_mix` return `audio` and `mix` plus bounded warnings for
these cases. Audio-only saves/exports are not supported; place at least one clip.
Audio replacement and mix updates are independent, preserve clips and other
state, and each successful call increments revision once. Importing videos and
replacing the timeline preserve audio and mix. Saves include all audio/mix fields;
exports retain their admission snapshot even if later edits change audio or mix.

Projects belong to one server session. Create starts revision 0; an import adding
assets or a successful timeline replacement increments revision. No-op imports
do not increment it. Create makes a project folder even if never saved. Save requires
clips and publishes `<output-root>/<slug>-<project_id>/<slug>.rNNNN.coveproj`.
NNNN is the edit-revision counter; the first save may be r0003. Media is referenced
in place, never copied: keep the source videos where they are. Publication is
via exclusive temp + descriptor-relative hard link. Earlier revisions are never
overwritten. Unchanged saves verify the recorded hash; external changes are
refused. Open the returned saved .coveproj using Project (menu button) -> Open Project..., rather than 'Add media files'.

Errors use bounded text and structured application errors; protocol errors remain SDK-owned.

| Code | Recovery |
|---|---|
| `INVALID_ARGUMENT`, `INVALID_TIMELINE` | Correct fields, trims or placement |
| `UNKNOWN_PROJECT`, `UNKNOWN_ASSET`, `ASSET_NOT_VIDEO` | Use ids returned by this session |
| `EMPTY_TIMELINE`, `LIMIT_EXCEEDED` | Place a clip or reduce the request |
| `NOT_CONFIGURED`, `PATH_NOT_AUTHORIZED` | Configure appropriate startup roots |
| `FILE_NOT_FOUND`, `NOT_A_REGULAR_FILE`, `UNSUPPORTED_EXTENSION`, `UNSUPPORTED_MEDIA` | Choose authorized supported media; added audio must decode and contain no real video |
| `PROBE_TIMEOUT`, `PROBE_FAILED`, `PROBE_OUTPUT_TOO_LARGE`, `PROBE_BUDGET_EXCEEDED` | Inspect locally or reduce the batch |
| `ASSET_CHANGED` | For audio, call cove_set_audio again with the complete replacement, then save; for video, create a fresh project and import again |
| `PROJECT_CHANGED_ON_DISK` | For export, call cove_set_timeline again for a new revision (or create a fresh project), then save and export; preserve the externally changed file |
| `DESTINATION_EXISTS` | Choose another output_name: export rejects an existing name at start, or at publication if it appears during rendering; for save, use a fresh project/revision |
| `OUTPUT_FILESYSTEM_UNSUPPORTED`, `OUTPUT_UNSAFE` | Fix the output directory/filesystem |
| `PROJECT_NOT_SAVED` | Save the current edit before starting an export |
| `EXPORT_BUSY` | Poll or cancel the active export, then retry |
| `UNKNOWN_EXPORT` | Use a recent job_id from this server session |
| `EXPORT_FAILED`, `EXPORT_OUTPUT_INVALID` | Check sources and FFmpeg; choose a new output_name only if that name now exists |
| `EXPORT_TIMEOUT` | Shorten the edit, or if you used encoder=auto, retry with encoder=cpu. |
| `SHUTTING_DOWN` | Ask the operator to restart the server. |

For export, `ASSET_CHANGED` uses the audio/video recovery above; audio errors
identify the affected item/id/path where available.
`DESTINATION_EXISTS` means choose another output name; existing files, directories
and symlinks (including dangling ones) are rejected before rendering. A name that
appears during rendering is rejected by the authoritative no-clobber hard link at
publication. Existing objects are preserved. Unsupported hard-link filesystems
have no overwrite fallback.

Typical session: operator asks “Trim my two videos, join them, and save an editable project.”
Verify: ask the agent to call `cove_get_capabilities`; check `media_roots`/`output_root`.
1. Agent inspects absolute paths, creates a named project and imports the videos.
2. Agent sets sequential clips with source trims in SECONDS, then gets the project to check it.
   If requested, set the complete background audio list with `cove_set_audio`,
   then update selected mix fields with `cove_set_audio_mix` and check the summary.
3. Agent saves the revision and returns its path for manual opening in Cove.
4. Agent calls `cove_start_export` with project_id and a new output_name, then
   polls `cove_get_export_status` until SUCCEEDED, FAILED or CANCELLED. Return the
   verified published path only on SUCCEEDED. Edits after admission do not change
   the bound saved revision/hash; save again before starting another export.

After export: open the saved .coveproj via Project (menu button) -> Open Project...,
never 'Add media files'. Play the MP4 at output.path in any media player;
'Add media files' may import that MP4 into Cove as a clip.

Export options: format `mp4` (H.264 + AAC); resolution `source` (first clip's native size), `3840x2160`,
`1920x1080`, `1280x720`, `1080x1920` or `1080x1080`; encoder `cpu` (default) or
`auto` (bounded hardware probes with CPU fallback). output_name must match
`^[A-Za-z0-9][A-Za-z0-9._-]{0,95}\.mp4$`. Output belongs to the server-created
project directory. Rendering uses a private directory with permissions 0700;
verified output is published by hard link, without clobbering any existing name.
Project directory permissions follow the operator's umask.

Export limits: one active job including preparation, 30 seconds preparation,
3600 seconds rendering, 5 seconds cancellation grace before killing the owned
child, 15 seconds shutdown, and 20 completed jobs retained. Errors are limited to
240 characters. Progress is an integer 0–100 from worker signals, or null before
any signal; no ETA is invented. A status error is in `error`; `output` is non-null
only on SUCCEEDED. `leftover` reports a non-final remnant that could not be cleaned.
Progress 100 does not mean finished: poll until SUCCEEDED, FAILED or CANCELLED.
A time-limit stop passes through CANCELLING and ends FAILED with EXPORT_TIMEOUT.

Use `cove_cancel_export` to stop an export. MCP protocol request cancellation is
separate. CANCELLING lasts until the child is reaped and cleanup finishes. The
VERIFYING-to-PUBLISHING transition commits publication: later cancel requests
leave the export completing truthfully and never delete a published output.
cancel_requested=false means cancellation was too late; the final state, not
the cancel call, is authoritative.

Candidate client configuration (add it yourself in your client; Cove registers nothing globally):
```json
{"mcpServers":{"cove":{"command":"/absolute/venv/bin/python","args":["-m","cove_video_editor.mcp_server","--media-root","/absolute/videos","--output-root","/absolute/projects"]}}}
```

Lifetime and shutdown: projects/assets exist only in server process memory.
Client EOF cancels active work, reaps its child and joins export threads before
closing project descriptors; exit 0. SIGTERM, SIGINT and SIGHUP use the same
shutdown path, then flush streams and exit with code 128 plus the signal number
(SIGTERM: 143, SIGINT: 130, SIGHUP: 129). EOF exits normally with code 0.
Signals received during shutdown are recorded; shutdown completes before exit 128+N.
Incomplete shutdown exits 1, or 128+N if a signal was recorded.
Signal exit bypasses interpreter thread shutdown after resource cleanup, so a
blocked transport stdin reader cannot keep the process alive. Saved revisions and published
outputs remain. SIGKILL or power loss can leave non-final `.export-*` workspaces
and children; there is no automatic recovery. Publication leaves no partial final
file, though a failure after linking (for example directory fsync failure) can
leave a complete visible file; choose a new name before retrying.
After a crash/SIGKILL, check for stray ffmpeg processes, then delete any .export-*
folder inside the project directory; leave the final .mp4.

Media is opened once, checked after open, and probed through its descriptor with
protocol/format whitelists. Export opens and holds one descriptor per asset; the
renderer uses `/dev/fd/N` for every video and added-audio input. MCP exports fail
closed if an added-audio descriptor is missing; they never reopen its pathname.
Video-only jobs keep the original video container whitelist. Jobs containing
added audio widen the job-wide whitelist with `mp3,wav,flac,ogg,aac`; these
demuxers also become allowed for that job's video inputs, which were already
content-probed under the stricter video whitelist at import. File-only protocol
restriction remains in every case. Disguised demuxers outside the appropriate
whitelist are blocked at render time as well as import.
Path replacement cannot redirect a held descriptor. Descriptors do not freeze
bytes: in-place changes are detected by comparing inode/size/mtime at completion.
These are application path checks, not an OS sandbox.
Same-user compromised processes, hard-linked files inside roots and untested
exotic demuxer behavior are out of scope. FFmpeg parses the media bytes.
A calibrated local file-open observer checks disguised concat rendering, including
an unrestricted positive control. It does not establish protection from every
demuxer or a compromised same-user process. Subtitles, crop, existing-project
import and packaged Windows execution remain future work.
