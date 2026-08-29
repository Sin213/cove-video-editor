"""Read and write ``.coveproj`` project documents.

A project file is a versioned, human-readable UTF-8 JSON document. It
carries exactly the *authoritative user edit state* of an editing session:
the media bin, the timeline clips, the added-audio entries, the subtitle
library and the audio-mix settings. Everything derived (thumbnail strips,
waveform peaks, parsed subtitle cues) and everything transient (selection,
playhead, crop draft, export run state) is deliberately absent - it is
rebuilt or reset on load.

Two rules shape the module:

* **Nothing but data crosses the boundary.** Loading uses ``json`` and
  hand-written field validation - no opaque object-graph format, no
  dynamic evaluation, no class-name dispatch. A hostile or corrupt file
  can produce a ``ProjectError``, never code execution.
* **Deserialization is a pure candidate build.** ``deserialize_project``
  constructs real ``MediaAsset`` / ``Clip`` / ``AddedAudio`` /
  ``SubtitleTrack`` objects off to the side and raises before returning
  anything partial. Callers therefore get either a complete replacement
  session or an exception - never a half-loaded timeline.

Widgets and file dialogs stay in ``app.py``; this module only knows about
paths and models. ``QSaveFile`` is the one Qt type it uses, for the write:
it stages into a sibling temp file and renames on commit, so a failure
mid-write leaves the previous project file exactly as it was rather than
truncating it.
"""
from __future__ import annotations

import json
import math
import re as _re
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QIODevice, QSaveFile

from .clip import (
    IMAGE_ASSET_DURATION_CAP,
    AddedAudio,
    Clip,
    MediaAsset,
    SubtitleTrack,
)

#: Marker identifying a document as ours. Checked before anything else so
#: a JSON file from another tool is rejected as the wrong format rather
#: than as a pile of missing fields.
FORMAT = "cove-video-editor-project"

#: Schema version. Bump on any incompatible change; readers refuse
#: anything they do not recognise instead of guessing.
SCHEMA_VERSION = 1

PROJECT_EXT = ".coveproj"

#: Ceiling on any persisted time value, in seconds.
#:
#: Not a taste judgement about project length - a bound the timeline can
#: actually address. ``TimelineWidget`` computes its scroll geometry as
#: ``int(total_seconds * pixels_per_second)`` and emits the result through
#: a signal typed as a C++ signed int, so at the maximum zoom of 800 px/s
#: anything past ``2**31 - 1`` pixels overflows. Crucially it overflows
#: *inside* ``_apply_project_state``, after the previous session has been
#: replaced, which is exactly the failure the transactional contract
#: exists to prevent - so the ceiling is enforced here instead.
#:
#: 30 days at 800 px/s is 2.07e9 px, inside the limit, and is orders of
#: magnitude beyond any real edit.
MAX_TIME_SECONDS = 2_592_000.0

#: Highest audio lane index a project may reference.
#:
#: Same reasoning as ``MAX_TIME_SECONDS``, on a different axis.
#: ``TimelineWidget._maintain_trailing_empty_lane`` grows its lane-height
#: list until it reaches ``highest_used + 2``, and every lane in that list
#: is painted, hit-tested and summed into the widget's minimum height on
#: each pass - so an unbounded index allocates and iterates without limit,
#: inside ``_apply_project_state`` and therefore after the previous
#: session is already gone.
#:
#: The bound is small on purpose. Lane 0 is the clip's own audio and the
#: UI adds lanes one at a time as the user drops onto the trailing empty
#: lane, so 64 lanes is already an order of magnitude past any real edit,
#: and at 48 px per lane the stack is ~3 000 px tall - unusable long
#: before the cap is reached. Nothing legitimate is excluded; a corrupt
#: file no longer decides how much memory Cove allocates.
MAX_AUDIO_LANE = 63

#: Clip playback-speed range. These are the clip-properties dialog's own
#: ``QDoubleSpinBox.setRange(0.25, 4.0)`` bounds, so nothing the editor can
#: produce is excluded.
#:
#: A speed of zero is the dangerous one: the exporter builds
#: ``setpts={1.0 / speed}*PTS`` and would raise ``ZeroDivisionError``.
#: Non-positive speeds also defeat ``MAX_TIME_SECONDS`` - ``Clip``
#: computes ``timeline_length`` as ``src_span / max(0.01, speed)``, so a
#: zero or negative speed multiplies a valid span by 100 and can push the
#: timeline back past the geometry limit.
MIN_CLIP_SPEED = 0.25
MAX_CLIP_SPEED = 4.0

#: Slack for the normalized crop invariant, absorbing the float error a
#: legitimate committed rect can carry (the crop overlay derives it from
#: view geometry) without admitting anything meaningfully outside frame.
_CROP_EPS = 1e-6

# --- remaining scalar domains ------------------------------------------
#
# Every value below is the domain of the production consumer that reads
# the field, not a guess. Type checking alone is not enough wherever the
# runtime accepts less than the type does: an out-of-domain value either
# reaches Qt and raises, or is silently clamped by a spin box and written
# back on the next save as if the user had chosen it.

#: Frame dimensions. Zero is legitimate and load-bearing - `_import_paths`
#: builds audio assets with `width=0, height=0, fps=0.0`. The ceiling is
#: the 16-bit dimension field that container and codec headers use, which
#: is the real limit on anything Cove can import, and it still admits 8K
#: sources several times over. These reach `VideoView.set_native_size`
#: and the crop pixel maths.
MAX_FRAME_DIMENSION = 65535

#: Frame rate. `MainWindow._current_fps` only trusts `fps > 0` and falls
#: back to 30, so zero stays legal for audio and image assets. The
#: ceiling is the practical container/codec maximum.
MAX_FPS = 1000.0

#: `MediaAsset.kind`, straight from the model docstring. It is dispatch,
#: not decoration: `_list_for_kind` picks the bin tab and
#: `_insert_clip_at` refuses anything that is not video or image.
ASSET_KINDS = ("video", "audio", "image")

#: Per-item gain. Both editors are percent controls over 0-200: the clip
#: properties dialog is `setRange(0, 200)` and the added-audio dialog is
#: `QInputDialog.getInt(..., 0, 200, 5)`.
MIN_ITEM_VOLUME = 0.0
MAX_ITEM_VOLUME = 2.0

#: Global mix gains, from `audio_gain` / `orig_gain`, both
#: `setRange(0.0, 3.0)`. These are the silent-clamp case: `setValue()`
#: quietly pulls an out-of-range number into the range and the next save
#: writes that back, so the project would differ from what was loaded.
MIN_MIX_GAIN = 0.0
MAX_MIX_GAIN = 3.0

#: Decimal places the mix-gain controls can hold, matching
#: ``QDoubleSpinBox``'s default ``decimals()``.
#:
#: These two are the only fields a load pushes straight into a widget:
#: ``_apply_project_state`` calls ``setValue()`` on both spin boxes, and a
#: spin box silently rounds to its precision, so a stored ``1.234`` would
#: come back as ``1.23`` and the next save would write that instead.
#: Speed, per-item volume and the trims stay on the model at load time
#: and only meet a widget if the user opens a dialog, which is an edit
#: rather than a load. A persistence test pins this constant to the real
#: widgets so the two cannot drift apart.
MIX_GAIN_DECIMALS = 2

#: Asset kinds a timeline clip may reference. `_insert_clip_at` accepts
#: only these two; an audio drop becomes an added-audio entry instead. A
#: clip on an audio asset would be handed to thumbnail generation and to
#: the exporter's video branch, neither of which has a stream to read.
CLIP_ASSET_KINDS = ("video", "image")

#: A still image carries its own, much tighter duration domain: the model
#: already defines the cap and the clip-properties dialog trims a still
#: against it, so the generic timeline ceiling does not apply. Aliased
#: from `clip.py` rather than restated, so the two cannot drift.
MAX_IMAGE_DURATION = float(IMAGE_ASSET_DURATION_CAP)

#: Most entries any one collection may hold.
#:
#: Applying a project starts up to two ffmpeg-backed analysis threads per
#: clip and one per added-audio entry, and it does so *after* the
#: outgoing session has been replaced. Media validation does not bound
#: this: a document can point any number of distinct ids at one valid
#: file, so the count itself has to be the check.
#:
#: 1000 is twenty times the app's own "this is a lot of files" line
#: (`FOLDER_IMPORT_WARN_THRESHOLD`, which only warns and still proceeds),
#: so no plausible edit is excluded, while the pathological documents -
#: the ones with hundreds of thousands of entries - are refused before
#: they can commit. This bounds the catastrophic case; genuinely bounding
#: analysis concurrency for a large-but-legal project is a worker
#: scheduling change and is deliberately not attempted here.
MAX_COLLECTION_ITEMS = 1000

#: `AddedAudio.rate` is a peaks-per-second cache. Its only producer is
#: `WaveformWorker.PEAK_RATE` (400) and its only consumer guards on
#: `rate > 0`, so the bound just has to keep an untrusted integer out of
#: the waveform paint maths; the audio-rate domain is the natural ceiling.
MAX_PEAK_RATE = 192000

#: Subtitle style, from the style dialog's own controls:
#: `font_size.setRange(10, 120)` and `outline.setRange(0, 8)`.
MIN_SUB_FONT_SIZE = 10
MAX_SUB_FONT_SIZE = 120
MIN_SUB_OUTLINE = 0
MAX_SUB_OUTLINE = 8

#: Subtitle sync offset. The sync dialog has two controls, and they are
#: not in conflict: `offset_slider` is a +/-5 s fine adjustment nested
#: inside `offset_spin`, and `current_offset_ms()` reads the *spin box*,
#: whose `setRange(-30.0, 30.0)` seconds is therefore the authority.
MAX_SUB_OFFSET_MS = 30_000

#: `SubtitleTrack.position`, from the model docstring. Chooses the libass
#: alignment (8 for top, 2 for bottom).
SUB_POSITIONS = ("bottom", "top")

#: Subtitle colours must be `#RRGGBB`. This one is not merely tidiness:
#: the two consumers disagree about what a malformed colour means.
#: `_parse_qcolor` falls back to white for the live preview, while the
#: exporter's `_hex_to_libass` only checks the length, so `"#GGGGGG"`
#: becomes the invalid libass literal `&H00GGGGGG&`. Rejecting the
#: document is what keeps preview and export describing the same edit.
_HEX_COLOR_RE = _re.compile(r"^#[0-9a-fA-F]{6}$")

#: Slack when comparing a stored source bound against a stored media
#: duration. Both come from the same probe, but they travel through spin
#: boxes and float arithmetic, so an exact comparison would reject a trim
#: that genuinely sits on the end of the media. One millisecond is far
#: below anything the editor can express and far above the error.
_TIME_EPS = 1e-3


class ProjectError(Exception):
    """A project document could not be read, parsed or validated.

    Every failure path raises this before any caller state is touched.
    """


@dataclass(slots=True)
class ProjectState:
    """A complete candidate session, built from real model objects.

    ``clips`` reference objects in ``assets`` by identity, exactly as the
    live session does, so applying a state never needs a second lookup
    table.
    """

    assets: list[MediaAsset] = field(default_factory=list)
    clips: list[Clip] = field(default_factory=list)
    added_audios: list[AddedAudio] = field(default_factory=list)
    subtitles: list[SubtitleTrack] = field(default_factory=list)
    replace_added_audio: bool = False
    added_gain: float = 1.0
    original_gain: float = 1.0


# --- typed field readers ------------------------------------------------
#
# Each raises ProjectError rather than coercing. A silently coerced field
# is a project that loads "successfully" with the wrong edit on it, which
# is worse than a refusal.


def _unique_ids(items: list, what: str) -> None:
    """Refuse a document that reuses an id inside one collection.

    Clip, added-audio and subtitle ids are the keys the app uses for
    worker registries, media players and selection. A duplicate would let
    one entry overwrite another's live runtime state, so the ambiguity is
    rejected during validation rather than half-applied.
    """
    seen: set[str] = set()
    for item in items:
        if item.id in seen:
            raise ProjectError(f"Duplicate {what} id '{item.id}'.")
        seen.add(item.id)


def _mapping(node: object, what: str) -> dict:
    if not isinstance(node, dict):
        raise ProjectError(f"{what} must be a JSON object.")
    return node


def _list(node: object, what: str) -> list:
    if not isinstance(node, list):
        raise ProjectError(f"{what} must be a JSON array.")
    if len(node) > MAX_COLLECTION_ITEMS:
        raise ProjectError(
            f"{what} holds {len(node)} entries, more than the supported "
            f"maximum of {MAX_COLLECTION_ITEMS}.")
    return node


def _num(node: dict, key: str, what: str, default: float | None = None) -> float:
    if key not in node:
        if default is None:
            raise ProjectError(f"{what} is missing '{key}'.")
        return default
    v = node[key]
    # `bool` is an `int` in Python; a true/false where a number belongs is
    # a malformed document, not a 1.
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ProjectError(f"{what} field '{key}' must be a number.")
    # `json.loads` accepts the NaN / Infinity literals. An infinite
    # timeline position survives every type check and only fails later,
    # when timeline geometry converts it to an int - long after the
    # previous session has been replaced. Reject it here, inside the
    # transactional boundary.
    if not math.isfinite(v):
        raise ProjectError(f"{what} field '{key}' must be a finite number.")
    return float(v)


def _ranged_num(node: dict, key: str, what: str, default: float | None,
                lo: float, hi: float) -> float:
    """A float held to ``[lo, hi]``.

    The shared shape for every bounded float: type-checked, non-finite
    rejected (by ``_num``), then held to the domain of whatever actually
    consumes it.
    """
    v = _num(node, key, what, default)
    if not (lo <= v <= hi):
        raise ProjectError(
            f"{what} field '{key}' is outside the supported range "
            f"({v} is not between {lo} and {hi}).")
    return v


def _ranged_int(node: dict, key: str, what: str, default: int,
                lo: int, hi: int) -> int:
    """An int held to ``[lo, hi]``. ``_int`` already refuses ``bool``."""
    v = _int(node, key, what, default)
    if not (lo <= v <= hi):
        raise ProjectError(
            f"{what} field '{key}' is outside the supported range "
            f"({v} is not between {lo} and {hi}).")
    return v


def _choice(node: dict, key: str, what: str, default: str,
            allowed: tuple[str, ...]) -> str:
    """A string from a closed set. Case-sensitive: these are stored keys,
    not user-facing text."""
    v = _str(node, key, what, default)
    if v not in allowed:
        raise ProjectError(
            f"{what} field '{key}' must be one of "
            f"{', '.join(allowed)} (got {v!r}).")
    return v


def _quantized_num(node: dict, key: str, what: str, default: float,
                   lo: float, hi: float, decimals: int) -> float:
    """A bounded float that the receiving control can hold exactly.

    Accepting a value with more precision than the widget keeps is a
    silent edit waiting to happen: the load rounds it and the next save
    writes the rounded number back.
    """
    v = _ranged_num(node, key, what, default, lo, hi)
    if round(v, decimals) != v:
        raise ProjectError(
            f"{what} field '{key}' has more precision than the control can "
            f"hold ({v} needs more than {decimals} decimal places).")
    return v


def _hex_color(node: dict, key: str, what: str, default: str) -> str:
    v = _str(node, key, what, default)
    if not _HEX_COLOR_RE.match(v):
        raise ProjectError(
            f"{what} field '{key}' must be a #RRGGBB colour (got {v!r}).")
    return v


def _time(node: dict, key: str, what: str, default: float | None = None) -> float:
    """A *signed* time offset. Only ``Clip.audio_offset`` is one: it is a
    drift correction and is legitimately negative."""
    return _ranged_num(node, key, what, default,
                       -MAX_TIME_SECONDS, MAX_TIME_SECONDS)


def _duration(node: dict, key: str, what: str,
              default: float | None = None) -> float:
    """A non-negative time: a duration, a timeline position, or a source
    bound. Nothing in the editor can produce a negative one - drags clamp
    with ``max(0.0, ...)`` and region edits shift within ``[0, ...]``."""
    return _ranged_num(node, key, what, default, 0.0, MAX_TIME_SECONDS)


def _require_id(node: dict, what: str) -> str:
    """A persisted id, required and non-empty.

    Defaulting a missing id to a fresh one would make the document change
    every time it is loaded and saved, and the empty string is the
    timeline's own no-selection sentinel, so an entry carrying it could
    not be selected, edited or deleted.
    """
    v = _str(node, "id", what)
    if not v:
        raise ProjectError(f"{what} field 'id' must not be empty.")
    return v


def _check_source_range(what: str, src_start: float, src_end: float,
                        media_duration: float) -> None:
    """Hold a source range to what the trim controls can express.

    ``__post_init__`` rewrites ``src_end <= 0`` to the media duration and
    ``src_span`` masks an inversion with ``max(0.001, ...)``, so an
    invalid range would not fail - it would load as a different project
    than the file describes.

    The ordering is ``<=`` rather than ``<`` on purpose:
    ``_append_added_audio`` falls back to ``dur = 0.0`` when the probe
    fails, which really does produce ``src_start == src_end == 0``.
    """
    if src_start > src_end:
        raise ProjectError(
            f"{what} source range is inverted "
            f"(src_start {src_start} is after src_end {src_end}).")
    # `__post_init__` reads `src_end <= 0` as "use the whole media" and
    # rewrites it. That is right for a freshly constructed clip and wrong
    # for a stored one: the project would load as a different edit than
    # the file describes. Only a genuinely zero-length medium may carry
    # it - which the probe-failure path really does produce.
    if src_end <= 0.0 < media_duration:
        raise ProjectError(
            f"{what} has an empty source range (src_end {src_end}) but its "
            f"media is {media_duration} seconds long; loading it would "
            f"silently expand the range to the whole medium.")
    if src_end > media_duration + _TIME_EPS:
        raise ProjectError(
            f"{what} source range ends past its media "
            f"(src_end {src_end} exceeds duration {media_duration}).")


def _speed(node: dict, what: str) -> float:
    """A clip playback speed, held to the editor's own supported range."""
    return _ranged_num(node, "speed", what, 1.0,
                       MIN_CLIP_SPEED, MAX_CLIP_SPEED)


def _lane(node: dict, what: str) -> int:
    """An audio lane index. Non-negative and bounded by ``MAX_AUDIO_LANE``.

    Checked here, not by letting the timeline discover it: the widget
    finds out how big a lane index is by allocating up to it.
    """
    return _ranged_int(node, "lane", what, 1, 0, MAX_AUDIO_LANE)


def _bool(node: dict, key: str, what: str, default: bool) -> bool:
    v = node.get(key, default)
    if not isinstance(v, bool):
        raise ProjectError(f"{what} field '{key}' must be true or false.")
    return v


def _int(node: dict, key: str, what: str, default: int) -> int:
    v = node.get(key, default)
    if isinstance(v, bool) or not isinstance(v, int):
        raise ProjectError(f"{what} field '{key}' must be an integer.")
    return v


def _str(node: dict, key: str, what: str, default: str | None = None) -> str:
    if key not in node:
        if default is None:
            raise ProjectError(f"{what} is missing '{key}'.")
        return default
    v = node[key]
    if not isinstance(v, str):
        raise ProjectError(f"{what} field '{key}' must be a string.")
    return v


def _path(node: dict, key: str, what: str) -> Path:
    raw = _str(node, key, what)
    if not raw:
        raise ProjectError(f"{what} field '{key}' must not be empty.")
    return Path(raw)


def _crop_rect(node: dict, what: str) -> tuple[float, float, float, float] | None:
    v = node.get("crop_rect", None)
    if v is None:
        return None
    if not isinstance(v, list) or len(v) != 4:
        raise ProjectError(f"{what} field 'crop_rect' must be four numbers.")
    out = []
    for n in v:
        if (isinstance(n, bool) or not isinstance(n, (int, float))
                or not math.isfinite(n)):
            raise ProjectError(
                f"{what} field 'crop_rect' must be four finite numbers.")
        out.append(float(n))
    x, y, w, h = out
    # `Clip.crop_rect` documents a normalized rectangle inside the frame.
    # It is not decoration: the preview and the exporter clamp an
    # out-of-domain rect differently, so a project carrying one would
    # restore an edit that looks one way and exports another - and it
    # would do so after the previous session had been replaced.
    if (
        x < -_CROP_EPS or y < -_CROP_EPS
        or w <= _CROP_EPS or h <= _CROP_EPS
        or w > 1.0 + _CROP_EPS or h > 1.0 + _CROP_EPS
        or x + w > 1.0 + _CROP_EPS or y + h > 1.0 + _CROP_EPS
    ):
        raise ProjectError(
            f"{what} field 'crop_rect' is not a normalized rectangle "
            f"inside the frame: {tuple(out)}.")
    return (x, y, w, h)


# --- serialization ------------------------------------------------------


def _asset_doc(a: MediaAsset) -> dict:
    return {
        "id": a.id,
        "path": str(a.path),
        "duration": a.duration,
        "width": a.width,
        "height": a.height,
        "fps": a.fps,
        "has_audio": a.has_audio,
        "kind": a.kind,
    }


def _clip_doc(c: Clip) -> dict:
    return {
        "id": c.id,
        "asset_id": c.asset.id,
        "timeline_start": c.timeline_start,
        "src_start": c.src_start,
        "src_end": c.src_end,
        "speed": c.speed,
        "muted": c.muted,
        "audio_volume": c.audio_volume,
        "linked_audio": c.linked_audio,
        "audio_offset": c.audio_offset,
        "audio_removed": c.audio_removed,
        "crop_rect": list(c.crop_rect) if c.crop_rect is not None else None,
        "crop_preset": c.crop_preset,
    }


def _audio_doc(a: AddedAudio) -> dict:
    return {
        "id": a.id,
        "path": str(a.path),
        "duration": a.duration,
        "rate": a.rate,
        "offset": a.offset,
        "lane": a.lane,
        "src_start": a.src_start,
        "src_end": a.src_end,
        "volume": a.volume,
        "muted": a.muted,
    }


def _sub_doc(s: SubtitleTrack) -> dict:
    return {
        "id": s.id,
        "path": str(s.path),
        "font_family": s.font_family,
        "font_size": s.font_size,
        "primary_color": s.primary_color,
        "outline_color": s.outline_color,
        "outline": s.outline,
        "position": s.position,
        "active": s.active,
        "offset_ms": s.offset_ms,
    }


def serialize_project(state: ProjectState) -> dict:
    """Build the project document for ``state``.

    Key order is fixed and the collections keep session order, so saving
    an unchanged session twice produces identical bytes.
    """
    return {
        "format": FORMAT,
        "version": SCHEMA_VERSION,
        "assets": [_asset_doc(a) for a in state.assets],
        "clips": [_clip_doc(c) for c in state.clips],
        "added_audio": [_audio_doc(a) for a in state.added_audios],
        "subtitles": [_sub_doc(s) for s in state.subtitles],
        "audio_mix": {
            "replace_added_audio": bool(state.replace_added_audio),
            "added_gain": float(state.added_gain),
            "original_gain": float(state.original_gain),
        },
    }


# --- deserialization ----------------------------------------------------


def deserialize_project(doc: object) -> ProjectState:
    """Validate ``doc`` and build the candidate session it describes.

    Raises ``ProjectError`` on anything unrecognised. Nothing is returned
    until every element has been constructed, so a caller can only ever
    receive a whole session.
    """
    root = _mapping(doc, "A project document")
    if root.get("format") != FORMAT:
        raise ProjectError(
            "This file is not a Cove Video Editor project.")
    if "version" not in root:
        raise ProjectError("This project has no schema version.")
    version = root["version"]
    if isinstance(version, bool) or not isinstance(version, int):
        raise ProjectError("This project has an invalid schema version.")
    if version != SCHEMA_VERSION:
        raise ProjectError(
            f"This project was written by a different version of Cove "
            f"(project schema {version}, supported {SCHEMA_VERSION}).")

    assets: list[MediaAsset] = []
    by_id: dict[str, MediaAsset] = {}
    for node in _list(root.get("assets", []), "'assets'"):
        node = _mapping(node, "An asset entry")
        # Kind first: it decides which duration domain applies.
        kind = _choice(node, "kind", "An asset", "video", ASSET_KINDS)
        duration_cap = (
            MAX_IMAGE_DURATION if kind == "image" else MAX_TIME_SECONDS
        )
        asset = MediaAsset(
            path=_path(node, "path", "An asset"),
            duration=_ranged_num(node, "duration", "An asset", 0.0,
                                 0.0, duration_cap),
            width=_ranged_int(node, "width", "An asset", 0,
                              0, MAX_FRAME_DIMENSION),
            height=_ranged_int(node, "height", "An asset", 0,
                               0, MAX_FRAME_DIMENSION),
            fps=_ranged_num(node, "fps", "An asset", 0.0, 0.0, MAX_FPS),
            has_audio=_bool(node, "has_audio", "An asset", False),
            kind=kind,
        )
        asset.id = _require_id(node, "An asset")
        if asset.id in by_id:
            raise ProjectError(f"Duplicate asset id '{asset.id}'.")
        assets.append(asset)
        by_id[asset.id] = asset

    clips: list[Clip] = []
    for node in _list(root.get("clips", []), "'clips'"):
        node = _mapping(node, "A clip entry")
        asset_id = _str(node, "asset_id", "A clip")
        asset = by_id.get(asset_id)
        if asset is None:
            raise ProjectError(
                f"A clip references media that is not in the project "
                f"(asset '{asset_id}').")
        if asset.kind not in CLIP_ASSET_KINDS:
            raise ProjectError(
                f"A clip references {asset.kind} media, which cannot sit on "
                f"the video timeline (only {' and '.join(CLIP_ASSET_KINDS)} "
                f"can).")
        # Read the source bounds and check them *before* constructing:
        # `Clip.__post_init__` rewrites `src_end <= 0`, so validating the
        # constructed object would inspect the value the model chose
        # rather than the one the document holds.
        src_start = _duration(node, "src_start", "A clip", 0.0)
        src_end = _duration(node, "src_end", "A clip", 0.0)
        _check_source_range("A clip", src_start, src_end, asset.duration)
        clip = Clip(
            asset=asset,
            timeline_start=_duration(node, "timeline_start", "A clip", 0.0),
            src_start=src_start,
            src_end=src_end,
            speed=_speed(node, "A clip"),
            muted=_bool(node, "muted", "A clip", False),
            audio_volume=_ranged_num(node, "audio_volume", "A clip", 1.0,
                                     MIN_ITEM_VOLUME, MAX_ITEM_VOLUME),
            linked_audio=_bool(node, "linked_audio", "A clip", True),
            audio_offset=_time(node, "audio_offset", "A clip", 0.0),
            audio_removed=_bool(node, "audio_removed", "A clip", False),
            crop_rect=_crop_rect(node, "A clip"),
            crop_preset=_str(node, "crop_preset", "A clip", "Free (Custom)"),
        )
        clip.id = _require_id(node, "A clip")
        clips.append(clip)

    added: list[AddedAudio] = []
    for node in _list(root.get("added_audio", []), "'added_audio'"):
        node = _mapping(node, "An added-audio entry")
        # Same ordering as clips: `AddedAudio.__post_init__` rewrites
        # `src_end <= 0`, so the document's own values are checked first.
        audio_duration = _duration(node, "duration", "Added audio", 0.0)
        audio_src_start = _duration(node, "src_start", "Added audio", 0.0)
        audio_src_end = _duration(node, "src_end", "Added audio", 0.0)
        _check_source_range("Added audio", audio_src_start, audio_src_end,
                            audio_duration)
        audio = AddedAudio(
            path=_path(node, "path", "Added audio"),
            duration=audio_duration,
            rate=_ranged_int(node, "rate", "Added audio", 0,
                             0, MAX_PEAK_RATE),
            offset=_duration(node, "offset", "Added audio", 0.0),
            lane=_lane(node, "Added audio"),
            src_start=audio_src_start,
            src_end=audio_src_end,
            volume=_ranged_num(node, "volume", "Added audio", 1.0,
                               MIN_ITEM_VOLUME, MAX_ITEM_VOLUME),
            muted=_bool(node, "muted", "Added audio", False),
        )
        audio.id = _require_id(node, "Added audio")
        added.append(audio)

    subs: list[SubtitleTrack] = []
    for node in _list(root.get("subtitles", []), "'subtitles'"):
        node = _mapping(node, "A subtitle entry")
        sub = SubtitleTrack(
            path=_path(node, "path", "A subtitle"),
            font_family=_str(node, "font_family", "A subtitle", "Arial"),
            font_size=_ranged_int(node, "font_size", "A subtitle", 36,
                                  MIN_SUB_FONT_SIZE, MAX_SUB_FONT_SIZE),
            primary_color=_hex_color(
                node, "primary_color", "A subtitle", "#FFFFFF"),
            outline_color=_hex_color(
                node, "outline_color", "A subtitle", "#000000"),
            outline=_ranged_int(node, "outline", "A subtitle", 2,
                                MIN_SUB_OUTLINE, MAX_SUB_OUTLINE),
            position=_choice(node, "position", "A subtitle", "bottom",
                             SUB_POSITIONS),
            active=_bool(node, "active", "A subtitle", False),
            offset_ms=_ranged_int(node, "offset_ms", "A subtitle", 0,
                                  -MAX_SUB_OFFSET_MS, MAX_SUB_OFFSET_MS),
        )
        sub.id = _require_id(node, "A subtitle")
        subs.append(sub)

    _unique_ids(clips, "clip")
    _unique_ids(added, "added-audio")
    _unique_ids(subs, "subtitle")

    # At most one subtitle burns in. `_activate_sub` clears every other
    # flag and the exporter takes the first active track, so a document
    # with two would leave the model, the bin, the preview and the export
    # disagreeing about which one is on.
    active_subs = [s for s in subs if s.active]
    if len(active_subs) > 1:
        raise ProjectError(
            f"Only one subtitle can be active at a time "
            f"({len(active_subs)} are marked active).")

    mix = _mapping(root.get("audio_mix", {}), "'audio_mix'")
    return ProjectState(
        assets=assets,
        clips=clips,
        added_audios=added,
        subtitles=subs,
        replace_added_audio=_bool(
            mix, "replace_added_audio", "'audio_mix'", False),
        added_gain=_quantized_num(mix, "added_gain", "'audio_mix'", 1.0,
                                  MIN_MIX_GAIN, MAX_MIX_GAIN,
                                  MIX_GAIN_DECIMALS),
        original_gain=_quantized_num(mix, "original_gain", "'audio_mix'", 1.0,
                                     MIN_MIX_GAIN, MAX_MIX_GAIN,
                                     MIX_GAIN_DECIMALS),
    )


def missing_media(state: ProjectState) -> list[Path]:
    """Every referenced source file that is not there any more.

    This slice stores real source paths and does no relinking, so the
    only question a load can answer is whether the file is still where
    the project says it is.
    """
    seen: list[Path] = []
    for p in (
        [a.path for a in state.assets]
        + [a.path for a in state.added_audios]
        + [s.path for s in state.subtitles]
    ):
        if p not in seen and not p.is_file():
            seen.append(p)
    return seen


def validate_media(state: ProjectState) -> None:
    gone = missing_media(state)
    if gone:
        names = "\n".join(str(p) for p in gone)
        raise ProjectError(f"Missing media files:\n{names}")


# --- file primitives ----------------------------------------------------


def normalize_project_path(path: Path) -> Path:
    """Give ``path`` the project extension unless it already has it.

    Case-insensitive, and only the final suffix is considered, so
    ``my.edit.v2`` becomes ``my.edit.v2.coveproj`` rather than losing a
    component.
    """
    if path.suffix.lower() == PROJECT_EXT:
        return path
    return path.with_name(path.name + PROJECT_EXT)


def read_project(path: Path) -> dict:
    """Read and JSON-parse ``path``. Raises ``ProjectError`` on any
    unreadable or malformed file."""
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise ProjectError(f"Could not read {path}: {exc}") from exc
    try:
        return json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ProjectError(f"{path} is not a valid UTF-8 text file.") from exc
    except json.JSONDecodeError as exc:
        raise ProjectError(f"{path} is not valid JSON: {exc}") from exc


def write_project(path: Path, doc: dict) -> None:
    """Write ``doc`` to ``path`` without ever truncating what is there.

    ``QSaveFile`` buffers into a sibling temp file and only renames over
    the target on ``commit()``. A failure at any earlier point cancels the
    write and removes the temp, so the previous project file survives a
    full disk or a lost mount intact rather than as a truncated stub.
    """
    # `allow_nan=False`: the NaN / Infinity literals are a JSON extension
    # that a reader is entitled to refuse - and this one does. Failing the
    # save is better than writing a project that cannot be opened again.
    try:
        data = json.dumps(doc, indent=2, ensure_ascii=False,
                          allow_nan=False).encode("utf-8")
    except ValueError as exc:
        raise ProjectError(f"Could not serialize the project: {exc}") from exc
    saver = QSaveFile(str(path))
    if not saver.open(QIODevice.WriteOnly | QIODevice.Truncate):
        raise ProjectError(
            f"Could not open {path} for writing: {saver.errorString()}")
    if saver.write(data) != len(data):
        saver.cancelWriting()
        raise ProjectError(f"Could not write {path}: {saver.errorString()}")
    if not saver.commit():
        raise ProjectError(f"Could not save {path}: {saver.errorString()}")


def save_project(path: Path, state: ProjectState) -> None:
    """Serialize then atomically write ``state``. Serialization happens
    first so a model problem is discovered before the file is touched."""
    write_project(Path(path), serialize_project(state))


def load_project(path: Path) -> ProjectState:
    """Read, validate and build the session in ``path``.

    The full sequence - parse, validate schema, construct models, check
    media - completes before anything is returned, which is what lets the
    caller treat a successful return as a safe commit point.
    """
    state = deserialize_project(read_project(Path(path)))
    validate_media(state)
    return state
