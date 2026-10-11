# Local MCP editing and export (M2b)

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
| `--media-root DIR` (repeatable) | Inspect/import videos in DIR and its subfolders |
| `--output-root DIR` | Create project folders, save revisions and publish exports in DIR |

No flags means no file access. The agent cannot change these permissions.

Agent edits live in memory until saved. Existing `.coveproj` editing, scene
understanding, music, subtitles, crop and live GUI control remain unavailable.

| Tool | Behavior |
|---|---|
| `cove_get_capabilities` | Permissions, operations and limits; no probes/writes |
| `cove_inspect_media` | Per-path video metadata or bounded error |
| `cove_create_project` | New server-owned project and exclusive directory |
| `cove_import_media` | Atomic import with identity deduplication |
| `cove_set_timeline` | Atomic replacement; sequential or explicit placement |
| `cove_get_project` | Assets, clips, revision, saved status and gap warnings |
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
| `FILE_NOT_FOUND`, `NOT_A_REGULAR_FILE`, `UNSUPPORTED_EXTENSION`, `UNSUPPORTED_MEDIA` | Choose an authorized supported video |
| `PROBE_TIMEOUT`, `PROBE_FAILED`, `PROBE_OUTPUT_TOO_LARGE`, `PROBE_BUDGET_EXCEEDED` | Inspect locally or reduce the batch |
| `ASSET_CHANGED` | Create a fresh project and import again |
| `PROJECT_CHANGED_ON_DISK` | For export, call cove_set_timeline again for a new revision (or create a fresh project), then save and export; preserve the externally changed file |
| `DESTINATION_EXISTS` | Choose another output_name: export rejects an existing name at start, or at publication if it appears during rendering; for save, use a fresh project/revision |
| `OUTPUT_FILESYSTEM_UNSUPPORTED`, `OUTPUT_UNSAFE` | Fix the output directory/filesystem |
| `PROJECT_NOT_SAVED` | Save the current edit before starting an export |
| `EXPORT_BUSY` | Poll or cancel the active export, then retry |
| `UNKNOWN_EXPORT` | Use a recent job_id from this server session |
| `EXPORT_FAILED`, `EXPORT_OUTPUT_INVALID` | Check sources and FFmpeg; choose a new output_name only if that name now exists |
| `EXPORT_TIMEOUT` | Shorten the edit, or if you used encoder=auto, retry with encoder=cpu. |
| `SHUTTING_DOWN` | Ask the operator to restart the server. |

For export, `ASSET_CHANGED` means re-import and re-save in a fresh project.
`DESTINATION_EXISTS` means choose another output name; existing files, directories
and symlinks (including dangling ones) are rejected before rendering. A name that
appears during rendering is rejected by the authoritative no-clobber hard link at
publication. Existing objects are preserved. Unsupported hard-link filesystems
have no overwrite fallback.

Typical session: operator asks “Trim my two videos, join them, and save an editable project.”
Verify: ask the agent to call `cove_get_capabilities`; check `media_roots`/`output_root`.
1. Agent inspects absolute paths, creates a named project and imports the videos.
2. Agent sets sequential clips with source trims in SECONDS, then gets the project to check it.
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
renderer uses `/dev/fd/N` with the same fixed file-only protocol and container
whitelists. Disguised demuxers are blocked at render time as well as import.
Path replacement cannot redirect a held descriptor. Descriptors do not freeze
bytes: in-place changes are detected by comparing inode/size/mtime at completion.
These are application path checks, not an OS sandbox.
Same-user compromised processes, hard-linked files inside roots and untested
exotic demuxer behavior are out of scope. FFmpeg parses the media bytes.
A calibrated local file-open observer checks disguised concat rendering, including
an unrestricted positive control. It does not establish protection from every
demuxer or a compromised same-user process. M3 features and packaged Windows
execution remain future work.
