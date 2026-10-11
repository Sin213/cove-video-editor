# Local MCP project editing (M2a)

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
| `--output-root DIR` | Create project folders and save revisions in DIR |

No flags means no file access. The agent cannot change these permissions.

What this does not do yet: export videos, add music/subtitles/crop, or open/edit
an existing `.coveproj`. Agent edits live in memory until saved.

| Tool | Behavior |
|---|---|
| `cove_get_capabilities` | Permissions, operations and limits; no probes/writes |
| `cove_inspect_media` | Per-path video metadata or bounded error |
| `cove_create_project` | New server-owned project and exclusive directory |
| `cove_import_media` | Atomic import with identity deduplication |
| `cove_set_timeline` | Atomic replacement; sequential or explicit placement |
| `cove_get_project` | Assets, clips, revision, saved status and gap warnings |
| `cove_save_project` | Immutable `.coveproj` revision and SHA-256 |

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
refused. Open the returned saved path with Cove's Open Project… command (Ctrl+O).

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
| `PROJECT_CHANGED_ON_DISK`, `DESTINATION_EXISTS` | Preserve that file; use a fresh project/revision |
| `OUTPUT_FILESYSTEM_UNSUPPORTED`, `OUTPUT_UNSAFE` | Fix the output directory/filesystem |

Typical session: operator asks “Trim my two videos, join them, and save an editable project.”
Verify: ask the agent to call `cove_get_capabilities`; check `media_roots`/`output_root`.
1. Agent inspects absolute paths, creates a named project and imports the videos.
2. Agent sets sequential clips with source trims in SECONDS, then gets the project to check it.
3. Agent saves the revision and returns its path for manual opening in Cove.

Candidate client configuration (add it yourself in your client; Cove registers nothing globally):
```json
{"mcpServers":{"cove":{"command":"/absolute/venv/bin/python","args":["-m","cove_video_editor.mcp_server","--media-root","/absolute/videos","--output-root","/absolute/projects"]}}}
```

Lifetime and shutdown: projects/assets exist only in server process memory.
Closing stdin or terminating the server discards them; saved files and published
revisions remain. Publication never leaves a partial final file. Normal probes
have a parent-enforced 10-second limit; abrupt kill can leave an in-flight
ffprobe child and prevents that timeout cleanup from running.

Media is opened once, checked after open, and probed through its descriptor with
protocol/format whitelists. These are application path checks, not an OS sandbox.
Same-user compromised processes, hard-linked files inside roots and untested
exotic demuxer behavior are out of scope. FFmpeg parses the media bytes.
DASH metadata and file-swap oracles detect weakened policies; FIFO/network access observation is unproven (default nested HTTP restrictions complicate it).
Export is unavailable (M2b); music, subtitles and crop editing are excluded (M3).
