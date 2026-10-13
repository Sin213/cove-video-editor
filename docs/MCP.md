# Local MCP editing and export (M3c)

Install the optional source extra with `pip install '.[mcp]'` in your chosen
environment. It pins `mcp==2.3.0`; the normal GUI neither needs nor imports MCP.
Install FFmpeg/ffprobe separately. Linux source installations are supported;
other platforms fail safely. Packaged builds have not been tested.

Launch using existing, absolute directories:

```sh
python -m cove_video_editor.mcp_server --media-root /absolute/video-directory --project-root /absolute/source-projects --output-root /absolute/output-directory
# Equivalent installed entry point: cove-video-editor-mcp
```

Permissions you must set: file operations need `--media-root` and/or
`--output-root` as appropriate; configure both for new-project editing, and
`--project-root` and `--output-root` to import existing projects, plus independent
`--media-root` authorization for their references. Project roots are optional
for new projects; the startup Notice names only roots that are missing and
notes that project roots are needed only to import existing projects.
The transport is local stdio only. stdout carries protocol messages; diagnostics
go to stderr. No GUI control, network listener, global configuration or defaults
to HOME/CWD are provided. Zero roots starts an unconfigured server. Invalid roots
exit 2; missing optional SDK exits 1. The output root must support hard links.

| Flag | File operations enabled |
|---|---|
| `--media-root DIR` (repeatable) | Inspect/import videos, add audio and read subtitles in DIR and its subfolders |
| `--project-root DIR` (repeatable, read-only) | Read existing `.coveproj` files only; does not authorize any referenced media |
| `--output-root DIR` | Create project folders, save revisions and publish exports in DIR |

No flags means no file access. The agent cannot change these permissions.

Agent edits live in memory until saved. `cove_open_project` imports an existing
`.coveproj` as a **new, independent agent-owned copy**. The original is never
modified, and referenced media is kept in place. Editing the original project
in place, relinking, scene understanding and live GUI control remain unavailable.
The output root is not an implicit project root: pass it explicitly as another
`--project-root` to reopen a saved agent revision.

| Tool | Behavior |
|---|---|
| `cove_get_capabilities` | Permissions, operations and limits; no probes/writes |
| `cove_inspect_media` | Per-path video metadata or bounded error |
| `cove_create_project` | New server-owned project and exclusive directory |
| `cove_open_project` | Existing authorized project imported as a new unsaved copy |
| `cove_import_media` | Atomic import with identity deduplication |
| `cove_set_timeline` | Atomic replacement; sequential or explicit placement |
| `cove_set_audio` | Atomic replacement of ALL added audio; `[]` clears it |
| `cove_set_audio_mix` | Partial mix update; omitted fields retain their values |
| `cove_set_subtitles` | Atomic replacement of ALL subtitle tracks; `[]` clears burn-in |
| `cove_set_crop` | Update one clip's crop with clip_id and expected_revision; null clears |
| `cove_get_project` | Assets, clips, audio, mix, revision, saved status and warnings |
| `cove_save_project` | Immutable `.coveproj` revision and SHA-256 |
| `cove_start_export` | Start rendering the current saved revision; return job_id |
| `cove_get_export_status` | Poll state, real progress, error and verified output |
| `cove_cancel_export` | Request cancellation; poll until terminal |

Inputs are strict, finite and reject extra fields. Limits: 50 paths/request,
4096 characters/path, 80/name, 20 projects/session, 200 assets/project, 200 clips,
86400 timeline seconds, 50 gap warnings. Probe limits: 10 seconds/file,
60 seconds/request, 65536 output bytes. Speed is 0.25–4; volume is 0–2.
Project snapshots are limited to 4 MiB (4194304 bytes), strict UTF-8 JSON,
unique object keys, finite numbers and at most 64 nested levels. BOMs and
unsupported schema versions are refused. The supported project schema is 1.
Only `.mp4`, `.mov`, `.mkv`, `.webm`, `.avi`, `.m4v` video files are accepted.
Explicit placement requires `timeline_start_s`; sequential placement forbids it.
Overlaps, invalid trims and missing asset ids are rejected without changing state.

`cove_open_project(path, name?)` requires an absolute `.coveproj` path within
startup project roots. Its optional name uses the same rules as create_project;
otherwise the sanitized file stem is used. Every asset, added-audio and subtitle
path must independently be authorized by a media root, **including inactive
subtitle tracks**. All references are authorized before any reference is opened
or probed; paths inside project files cannot add roots or FFmpeg options.
Names like `subtitles[1].path` index arrays in the `.coveproj` JSON (zero-based);
the operator inspects and corrects the file locally.
Relative paths, URLs, schemes, drive/backslash paths, `.`, `..`, repeated separators
and trailing slashes are refused; there is no resolution against CWD. Raw and
resolved paths must be valid Unicode without surrogate code points. Symlinks
that resolve to valid Unicode paths inside media roots are accepted, stored canonically and counted in
a warning; escapes are refused.

Video and audio references are re-probed through held descriptors. Stored
width/height/audio flags must match exactly; duration tolerance is 0.001 seconds
and FPS absolute tolerance is 0.001. Accepted stored values are retained, not
silently replaced. Source trims must also fit the probed duration plus tolerance.
Image/audio-kind assets and overlapping video clips are unsupported features:
remove or replace them in Cove, save, then retry. Timelines over 86400 seconds,
collections over the limits above, unsafe identifiers/fonts and subtitle tracks
with no valid cues are refused atomically. Imported ids may not contain `:`:
it is reserved for export keys. Every subtitle uses the same bounded
snapshot and cue rules as cove_set_subtitles. All supported trims, speed, original
audio controls, added-audio lanes/offsets/mute/volume, mix, subtitle style/activation
and per-clip crop state survive import. Warnings explain skipped malformed cue
blocks and audio beginning at/after the timeline end; correct the latter before save.

Each successful call returns a fresh `project_id`, revision 1, `saved_revision:null`,
counts, crops, warnings and `imported` provenance (`source_path`, `source_sha256`,
`source_size_bytes`, `schema_version`, `copy_on_import:true`). Repeated imports
create separate copies and consume separate slots in the 20-project limit.
Validation completes before allocating the new output directory. Failed imports
consume no slot and write no project or output file. Use existing editing tools
with the returned id, then save the current revision before exporting.

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

The editing workflow can add `cove_set_subtitles` and `cove_set_crop`
after setting the timeline, then read the project, save and export as usual.
Subtitles are burn-in only: no soft subtitles or sidecar output. Send a complete
`tracks` list to replace, restyle or disable subtitles; `[]` clears them. Each
track requires an absolute authorized `.srt` or `.vtt` path. ASS/SSA are refused
because Cove renders only cues parsed by its existing SRT/VTT parser.
Files must be strict UTF-8 (BOM accepted), at most 1 MiB, with 1–5000 valid cues,
at most 1000 characters per cue and cue end at most 86400 seconds. NUL and
control characters except tab/newline/carriage return are refused. Valid partial
files are accepted; `skipped_cue_blocks` reports malformed timing blocks. Summary
also includes cue_count, first_cue_s, last_cue_s and server-generated subtitle_id.
Cue timestamps must include hours: `hh:mm:ss,mmm` (SRT) or `hh:mm:ss.mmm`
(VTT). Short-form VTT `mm:ss.mmm` cues are not recognised by Cove's shared
parser; mixed files report them in skipped_cue_blocks and a warning. A file
with no recognised cues is rejected. Check skipped_cue_blocks after import.
Parser safety caps also reject more than 5000 `-->` markers, timing lines
over 256 characters, blocks over 8192 characters, and more than 16 combined
unterminated `<`/`{` markers after each block's last corresponding closer.
Ordinary literal text such as `I <3 you` is accepted without modification.

There are at most 8 tracks and at most one active. **active defaults to true**;
explicit false disables burn-in. Defaults: font_family Arial, font_size 36,
primary_color #FFFFFF, outline_color #000000, outline 2, position bottom,
offset_ms 0. Font size is integer 10–120; outline integer 0–8; colours #RRGGBB;
position bottom or top; offset integer -30000–30000 milliseconds. Positive offset
delays cues. Font names are 1–64 letters/digits/spaces/period/underscore/hyphen,
starting with a letter/digit. The named font must be installed; Linux libass
falls back if unavailable, so Unicode glyph appearance depends on installed fonts.
No-active and active-outside-timeline warnings explain exports without text.

`cove_get_project` and `cove_set_timeline` expose `clips[].clip_id`, `crop` and
`crop_pixels`. Read the current `revision`, then call
`cove_set_crop(project_id, clip_id, expected_revision, crop)`. The crop object
requires x, y, width, height normalized to the **source** frame, independent of
trim, playback speed or output size. The origin is the source's TOP-LEFT corner;
x increases rightward and y downward. For a 1920x1080 source, the right half is
`{x:0.5,y:0,width:0.5,height:1}`, giving crop_pixels
`{x:960,y:0,width:960,height:1080}`. Crop pixel width/height are even; x/y may
be odd when clamped against an odd-sized source edge. Pixel conversion uses
Cove's existing rounding. Invalid, full-frame and ineffective crops are refused without
clamping; send required `crop:null` to clear. Only the addressed clip changes.
In per-clip mode the output canvas follows the FIRST clip's effective geometry;
a narrower later clip is fitted and padded into that canvas.

`set_timeline` replaces all clips and clears existing crops. Its `cleared_crops`
list reports previous clip_id, asset_id, source trim and crop, accompanied by a
warning to re-apply crops with the new ids. Other calls return an empty list.
Successful subtitle/crop calls increment revision once; failed calls preserve
the entire prior state. REVISION_MISMATCH reports the current revision: re-read
`cove_get_project`. INVALID_SUBTITLE: correct the file and resubmit the tracks.
ASSET_CHANGED for subtitles: call `cove_set_subtitles` again, then save.
PATH_NOT_AUTHORIZED: check startup media_roots; callers cannot widen permissions.

Subtitle files are read from held authorized regular-file descriptors, bounded
and hashed before parsing the in-memory text with the existing parser. Every
save and export admission reopens each subtitle under the same root policy and
requires matching identity AND SHA-256. A descriptor prevents path substitutions
from redirecting the read; it does not freeze in-place edits. Hash rechecks at
save/export-start detect those edits. The export uses an independent snapshot of
cues, styles and crop, so later edits cannot change a running render. Saves
persist paths/styles/offsets; the GUI reparses referenced subtitle files on open.
FFmpeg reads only the generated ASS in a private 0700 `cove-subs-*` directory,
removed after success, failure or cancellation. SIGKILL can leave that directory.
Names, paths and subtitle text are data, never instructions. These boundaries
are Linux-only application checks, **not an OS sandbox**.

Projects belong to one server session. Create starts revision 0; an import adding
assets or a successful timeline replacement increments revision. No-op imports
do not increment it. Create makes a project folder even if never saved. Save requires
clips and publishes `<output-root>/<slug>-<project_id>/<slug>.rNNNN.coveproj`.
NNNN is the edit-revision counter; the first save may be r0003. Media is referenced
in place, never copied: keep source videos, audio and subtitle files where they are. Publication is
via exclusive temp + descriptor-relative hard link. Earlier revisions are never
overwritten. Unchanged saves verify the recorded hash; external changes are
refused. Open the returned saved .coveproj using Project (menu button) -> Open Project..., rather than 'Add media files'.
Alternatively, run `PYTHONPATH=src python3 -m cove_video_editor --project "/absolute/path/revision.coveproj"`.
Run this from the repository checkout root so `PYTHONPATH=src` finds its `src` directory.
This is Linux/macOS shell syntax, exercised only on Linux; for the project venv,
replace `python3` with `.venv/bin/python`.
GUI Save overwrites that file; use Project -> Save As to preserve the MCP revision.

Errors use bounded text and structured application errors; protocol errors remain SDK-owned.

| Code | Recovery |
|---|---|
| `INVALID_ARGUMENT`, `INVALID_TIMELINE` | Correct fields, trims or placement |
| `INVALID_PROJECT` | Correct malformed JSON, unsupported schema version, field types, identifiers or relationships locally; supported schema is 1 |
| `UNSUPPORTED_PROJECT_FEATURE` | Image/audio-kind assets and overlapping clips cannot be imported; remove or replace them in Cove, save, then retry |
| `PROJECT_REFERENCE_INVALID` | Correct non-absolute/non-normalized references or stored metadata locally; relinking is unsupported |
| `UNKNOWN_PROJECT`, `UNKNOWN_ASSET`, `ASSET_NOT_VIDEO` | Use ids returned by this session |
| `INVALID_SUBTITLE` | Check UTF-8, parser limits and timestamps `hh:mm:ss,mmm` / `hh:mm:ss.mmm`; resubmit complete tracks |
| `INVALID_CROP` | Use an effective rectangle inside the source frame; use crop=null to clear |
| `UNKNOWN_CLIP` | Re-read cove_get_project for current clip_ids |
| `REVISION_MISMATCH` | Re-read cove_get_project; bind the next crop to its current revision |
| `EMPTY_TIMELINE`, `LIMIT_EXCEEDED` | Place a clip or reduce the request |
| `NOT_CONFIGURED`, `PATH_NOT_AUTHORIZED` | Ask the operator to restart with appropriate startup roots: project_roots authorize project files; media_roots separately authorize every reference |
| `FILE_NOT_FOUND`, `NOT_A_REGULAR_FILE`, `UNSUPPORTED_EXTENSION`, `UNSUPPORTED_MEDIA` | Choose authorized supported media; added audio must decode and contain no real video |
| `PROBE_TIMEOUT`, `PROBE_FAILED`, `PROBE_OUTPUT_TOO_LARGE`, `PROBE_BUDGET_EXCEEDED` | Inspect locally or reduce the batch |
| `ASSET_CHANGED` | For audio, call cove_set_audio again with the complete replacement, then save; for video, create a fresh project and import again; for an imported project, call cove_open_project again |
| `ASSET_CHANGED` (subtitle) | Re-submit the COMPLETE track list with cove_set_subtitles even if the file looks unchanged, then save: identity AND SHA-256 are rechecked; a byte-identical replacement or touched file still counts as changed |
| `PROJECT_CHANGED_ON_DISK` | For export, call cove_set_timeline again for a new revision (or create a fresh project), then save and export; preserve the externally changed file |
| `DESTINATION_EXISTS` | Choose another output_name: export rejects an existing name at start, or at publication if it appears during rendering; for save, use a fresh project/revision |
| `OUTPUT_FILESYSTEM_UNSUPPORTED`, `OUTPUT_UNSAFE` | Fix the output directory/filesystem |
| `PROJECT_NOT_SAVED` | Save the current edit before starting an export |
| `EXPORT_BUSY` | Poll or cancel the active export, then retry |
| `UNKNOWN_EXPORT` | Use a recent job_id from this server session |
| `EXPORT_FAILED`, `EXPORT_OUTPUT_INVALID` | Check sources and FFmpeg; choose a new output_name only if that name now exists |
| `EXPORT_TIMEOUT` | Shorten the edit, or if you used encoder=auto, retry with encoder=cpu. |
| `SHUTTING_DOWN` | Ask the operator to restart the server. |

For export, `ASSET_CHANGED` uses the audio/video/subtitle recovery above; errors
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
   If requested, replace subtitle tracks with `cove_set_subtitles` and set each
   clip's crop with `cove_set_crop`, using clip_id and the latest revision.
   Every successful edit returns the new revision; use it for the next set_crop.
3. Agent saves the revision and returns its path for manual opening in Cove.
4. Agent calls `cove_start_export` with project_id and a new output_name, then
   polls `cove_get_export_status` until SUCCEEDED, FAILED or CANCELLED. Return the
   verified published path only on SUCCEEDED. Edits after admission do not change
   the bound saved revision/hash; save again before starting another export.

After export: open the saved .coveproj via Project (menu button) -> Open Project...,
never 'Add media files'. Play the MP4 at output.path in any media player;
'Add media files' may import that MP4 into Cove as a clip.
Alternatively, open the project with `PYTHONPATH=src python3 -m cove_video_editor --project "/absolute/path/revision.coveproj"`.
Run this from the repository checkout root so `PYTHONPATH=src` finds its `src` directory.
This is Linux/macOS shell syntax, exercised only on Linux; for the project venv,
replace `python3` with `.venv/bin/python`.
Use Project -> Save As to preserve the MCP revision; GUI Save overwrites the opened file.

For an existing project, manually verify this sequence:
1. Check capabilities for project_roots, media_roots and output_root. Record the
   original project's SHA-256 locally, then call cove_open_project with its absolute path.
2. Check the returned provenance, counts, crops and warnings, and call
   cove_get_project to inspect the preserved edit. Missing media or unauthorized
   paths require operator intervention; no MCP tool can expand permissions.
3. Apply requested edits, save the new immutable revision and export it with a
   fresh MP4 filename. Open the saved copy manually in Cove to verify its state.
4. Compare the original SHA-256 again; it must be unchanged. A second import
   should return a different id and independent output directory.

Export options: format `mp4` (H.264 + AAC); resolution `source` (first clip's effective cropped size), `3840x2160`,
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
{"mcpServers":{"cove":{"command":"/absolute/venv/bin/python","args":["-m","cove_video_editor.mcp_server","--media-root","/absolute/videos","--project-root","/absolute/source-projects","--output-root","/absolute/projects"]}}}
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
Authorization canonicalizes with realpath (symlink metadata is read before
containment is decided); an outside file can be opened for descriptor
re-containment but is never read/probed, as inherited from M2 media opening.
Same-user compromised processes, hard-linked files inside roots and untested
exotic demuxer behavior are out of scope. FFmpeg parses the media bytes.
A calibrated local file-open observer checks disguised concat rendering, including
an unrestricted positive control. It does not establish protection from every
demuxer or a compromised same-user process. Existing-project import retains
these Linux-only application checks; packaged Windows execution remains future work.
