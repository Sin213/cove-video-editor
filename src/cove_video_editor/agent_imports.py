"""Bounded adaptation of the existing project parser to agent invariants."""
from __future__ import annotations
import json, math, os, re, unicodedata
from . import agent_paths as ap
from .project_io import ProjectState, ProjectError, deserialize_project, SCHEMA_VERSION, _TIME_EPS

# GUI and agent probes use the same ffprobe format duration and r_frame_rate.
# Seven synthetic fixtures (incl. fractional fps and compressed audio) measured
# zero delta. Retain project_io's established millisecond source-range allowance
# for persisted values and ffprobe's six-decimal durations (Addendum 2).
DURATION_TOLERANCE, FPS_TOLERANCE = _TIME_EPS, 1e-3
def _fail(code, field, reason):
    raise ap.AgentError(code, f"{field}: {reason}")
def validate_output_path(path, field):
    try: path.encode("utf-8", errors="strict")
    except UnicodeError:
        _fail("PROJECT_REFERENCE_INVALID", field, "Path must be valid Unicode without surrogate code points.")
def reference_text(path, field):
    try: ap.reject_unsafe_path_text(path)
    except ap.AgentError:
        _fail("PROJECT_REFERENCE_INVALID", field, "Use a normalized absolute local path; no URLs, schemes or backslashes.")
    validate_output_path(path, field)
    if os.path.normpath(path) != path:
        _fail("PROJECT_REFERENCE_INVALID", field, "Path must be normalized; no '.', '..', repeated separators or trailing slash.")
def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result: raise ValueError("duplicate JSON key")
        result[key] = value
    return result
def _constant(value):
    raise ValueError("non-finite JSON constant")
def parse_project_snapshot(data: bytes) -> ProjectState:
    """Only strict decoding and boundary adaptation; project_io owns the format."""
    try:
        doc = json.loads(data.decode("utf-8", errors="strict"), object_pairs_hook=_pairs, parse_constant=_constant)
        pending = [(doc, 0)]
        while pending:
            node, depth = pending.pop()
            if depth > 64: raise ValueError("JSON depth")
            if isinstance(node, dict): pending.extend((v, depth+1) for v in node.values())
            elif isinstance(node, list): pending.extend((v, depth+1) for v in node)
            elif isinstance(node, float) and not math.isfinite(node): raise ValueError("non-finite number")
    except (UnicodeError, ValueError, RecursionError, OverflowError):
        _fail("INVALID_PROJECT", "project", "Require strict UTF-8 JSON, unique keys, finite numbers and depth <=64.")
    if isinstance(doc, dict):
        version = doc.get("version")
        if type(version) is int and version != SCHEMA_VERSION:
            _fail("INVALID_PROJECT", "version", f"Schema {str(version)[:40]} is unsupported; supported version is {SCHEMA_VERSION}.")
        for key, maximum in (("assets",ap.MAX_ASSETS), ("clips",ap.MAX_CLIPS),
                             ("added_audio",ap.MAX_AUDIO_ITEMS), ("subtitles",ap.MAX_SUBTITLE_TRACKS)):
            nodes = doc.get(key, [])
            if type(nodes) is list:
                if len(nodes) > maximum: _fail("LIMIT_EXCEEDED", key, f"At most {maximum} entries.")
                for i, node in enumerate(nodes):
                    if not isinstance(node, dict): continue
                    if key == "assets" and node.get("kind", "video") in ("audio", "image"):
                        _fail("UNSUPPORTED_PROJECT_FEATURE", f"assets[{i}].kind", "Only video assets are supported.")
                    if key != "clips" and "path" in node: reference_text(node["path"], f"{key}[{i}].path")
    try: state = deserialize_project(doc)
    except (ProjectError, RecursionError, OverflowError, ValueError):
        # ProjectError can contain attacker-controlled ids/values. Never echo it.
        _fail("INVALID_PROJECT", "project fields", "Invalid format, version, field type, value or relationship; supported version is 1.")
    validate_state(state)
    return state
def enumerate_references(state):
    for key, kind, items in (("assets","video",state.assets), ("added_audio","audio",state.added_audios),
                             ("subtitles","subtitle",state.subtitles)):
        for i, item in enumerate(items): yield kind, f"{key}[{i}].path", str(item.path)
def _bounded_string(value, field, maximum=80):
    if not 1 <= len(value) <= maximum or any(unicodedata.category(c).startswith("C") for c in value):
        _fail("INVALID_PROJECT", field, f"Require 1–{maximum} characters without controls.")
def validate_state(state):
    for key, items in (("assets",state.assets), ("clips",state.clips), ("added_audio",state.added_audios),
                       ("subtitles",state.subtitles)):
        for i, item in enumerate(items):
            _bounded_string(item.id, f"{key}[{i}].id")
            if ":" in item.id:
                _fail("INVALID_PROJECT", f"{key}[{i}].id", "':' is reserved for export keys; imported ids must not contain it.")
    for i, a in enumerate(state.assets):
        if a.kind != "video": _fail("UNSUPPORTED_PROJECT_FEATURE", f"assets[{i}].kind", "Only video assets are supported.")
        if not (0 < a.duration <= 2592000 and 1 <= a.width <= 65535 and 1 <= a.height <= 65535 and 0 <= a.fps <= 1000):
            _fail("INVALID_PROJECT", f"assets[{i}]", "Metadata cannot be represented by agent tools.")
    ordered = sorted(state.clips, key=lambda c:c.timeline_start)
    if any(a.timeline_end > b.timeline_start for a,b in zip(ordered,ordered[1:])):
        _fail("UNSUPPORTED_PROJECT_FEATURE", "clips", "Overlapping video clips are unsupported.")
    for i, c in enumerate(state.clips):
        _bounded_string(c.crop_preset, f"clips[{i}].crop_preset")
        if c.src_end <= c.src_start: _fail("INVALID_PROJECT", f"clips[{i}].src_end", "Source trim must be nonempty.")
        if c.timeline_end > ap.MAX_TIMELINE: _fail("LIMIT_EXCEEDED", f"clips[{i}].timeline_end", "Timeline exceeds 86400 seconds.")
    for i, a in enumerate(state.added_audios):
        if a.timeline_end > ap.MAX_TIMELINE: _fail("LIMIT_EXCEEDED", f"added_audio[{i}].timeline_end", "Timeline exceeds 86400 seconds.")
        if not (0 < a.duration <= ap.MAX_TIMELINE and a.src_start < a.src_end <= ap.MAX_TIMELINE):
            _fail("INVALID_PROJECT", f"added_audio[{i}]", "Audio duration/trim cannot be represented by agent tools.")
    for i, s in enumerate(state.subtitles):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}", s.font_family):
            _fail("INVALID_PROJECT", f"subtitles[{i}].font_family", "Require a safe font name of 1–64 characters.")
        for field in ("primary_color", "outline_color"):
            if not re.fullmatch(r"#[0-9A-Fa-f]{6}", getattr(s, field)):
                _fail("INVALID_PROJECT", f"subtitles[{i}].{field}", "Require exactly #RRGGBB with ASCII hex digits.")
def validate_metadata(item, info, field):
    fields = [("duration","duration_s",DURATION_TOLERANCE)]
    if info["kind"] == "video":
        fields += [("width","width",0), ("height","height",0), ("fps","fps",FPS_TOLERANCE), ("has_audio","has_audio",0)]
    for stored, measured, tolerance in fields:
        value, actual = getattr(item, stored), info[measured]
        if value < actual - tolerance or value > actual + tolerance:
            _fail("PROJECT_REFERENCE_INVALID", field.rsplit(".",1)[0]+"."+stored, "Stored metadata does not match the probed source.")
