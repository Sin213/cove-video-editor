"""Server-owned edits; candidates validate before becoming visible."""
from __future__ import annotations
import hashlib, math, os, re, stat, threading, time, unicodedata, uuid
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from . import agent_paths as ap
from .clip import AddedAudio, Clip, MediaAsset
from .project_io import (ProjectState, ProjectError, serialize_project, deserialize_project,
    _check_source_range, MIN_CLIP_SPEED, MAX_CLIP_SPEED, MIN_ITEM_VOLUME, MAX_ITEM_VOLUME,
    MIN_MIX_GAIN, MAX_MIX_GAIN, MIX_GAIN_DECIMALS)
_UNSET = object()
@dataclass
class Project:
    name: str
    slug: str
    dirname: str
    fd: int
    state: ProjectState = field(default_factory=ProjectState)
    identities: dict = field(default_factory=dict)
    audio_identities: dict = field(default_factory=dict)
    revision: int = 0
    saved: dict | None = None
def _number(value, low=0, high=ap.MAX_TIMELINE, label="number"):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ap.AgentError("INVALID_ARGUMENT", f"{label} must be finite and strictly typed.")
    if not low <= value <= high:
        raise ap.AgentError("INVALID_TIMELINE", f"{label} {value:g} is outside {low:g}–{high:g}.")
    return float(value)
@contextmanager
def _context(label):
    try:
        yield
    except ap.AgentError as exc:
        raise ap.AgentError(exc.code, f"{label}: {exc.message}") from None
def _validated(state: ProjectState) -> ProjectState:
    try:
        doc = serialize_project(state)
        candidate = deserialize_project(doc)
        if serialize_project(candidate) != doc:
            raise ProjectError("Round trip changed the edit.")
        return candidate
    except ProjectError:
        raise ap.AgentError("INVALID_TIMELINE", "Project validation failed.") from None
def _asset(a: MediaAsset) -> dict:
    return dict(asset_id=a.id, path=str(a.path), duration_s=a.duration, width=a.width,
                height=a.height, fps=a.fps, has_audio=a.has_audio)
class AgentSession:
    def __init__(self, policy: ap.PathPolicy):
        self.policy = policy
        self.lock = threading.RLock()
        self.projects: dict[str, Project] = {}
        self.closing = False
        from .agent_exports import ExportManager
        self.exports = ExportManager(self)
    def close(self) -> None:
        self.shutdown_exports()
        with self.lock:
            for p in self.projects.values():
                os.close(p.fd)
            self.projects.clear()
            self.policy.close()
    def shutdown_exports(self) -> None:
        self.exports.shutdown()
    def start_export(self, project_id, output_name, format="mp4", resolution="source", encoder="cpu") -> dict:
        return self.exports.start(project_id, output_name, format, resolution, encoder)
    def get_export_status(self, job_id) -> dict:
        return self.exports.status(job_id)
    def cancel_export(self, job_id) -> dict:
        return self.exports.cancel(job_id)
    def capabilities(self) -> dict:
        return dict(operations=["cove_" + s for s in ("get_capabilities", "inspect_media", "create_project",
            "import_media", "set_timeline", "get_project", "save_project", "start_export",
            "get_export_status", "cancel_export", "set_audio", "set_audio_mix")], media_kinds=["video", "audio"],
            extensions=sorted(ap.EXTENSIONS), audio_extensions=sorted(ap.AUDIO_EXTENSIONS),
            supported=["video timeline", "background audio", "mix controls", "save", "export"],
            deferred=["subtitles", "crop", "open_project"], media_roots=list(self.policy.media_roots),
            output_root=self.policy.output_root, limits=ap.LIMITS, save_policy="immutable revisions",
            publication="descriptor-relative hard link", export=self.exports.capabilities())
    def _project(self, pid: str) -> Project:
        if type(pid) is not str or len(pid) > 80:
            raise ap.AgentError("INVALID_ARGUMENT", "Use an opaque project id.")
        if pid not in self.projects:
            raise ap.AgentError("UNKNOWN_PROJECT", "Unknown project_id. The server may have restarted; unsaved projects are lost - call cove_create_project.")
        return self.projects[pid]
    def _paths(self, paths) -> None:
        if type(paths) is not list or not 1 <= len(paths) <= ap.MAX_PATHS:
            raise ap.AgentError("LIMIT_EXCEEDED", "Supply between 1 and 50 paths.")
        for i, path in enumerate(paths):
            with _context(f"paths[{i}] {repr(path)[:100]}"):
                ap.reject_unsafe_path_text(path)
    def _probe_at(self, i, path, deadline, known=None):
        with _context(f"paths[{i}] {repr(path)[:100]}"):
            return self._probe(path, deadline, known)
    def _probe(self, path: str, deadline: float, known=None, *, kind="video"):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ap.AgentError("PROBE_BUDGET_EXCEEDED", "Request probe budget exhausted.")
        options = {"kind": "audio"} if kind == "audio" else {}
        with ap.open_authorized_media(self.policy, path, **options) as (fd, canonical, identity):
            if known and identity[:2] in known and known[identity[:2]] != identity:
                raise ap.AgentError("ASSET_CHANGED", "Previously imported inode changed; create a fresh project.")
            info = ap.probe_fd(fd, timeout=min(ap.PROBE_TIMEOUT, remaining), **options)
            if ap.file_identity(os.fstat(fd)) != identity:
                raise ap.AgentError("ASSET_CHANGED", "Media changed while probing.")
            return canonical, identity, info
    def inspect_media(self, paths: list[str]) -> dict:
        if type(paths) is not list or not 1 <= len(paths) <= ap.MAX_PATHS:
            raise ap.AgentError("LIMIT_EXCEEDED", "Supply between 1 and 50 paths.")
        results, deadline = [], time.monotonic() + ap.PROBE_BUDGET
        for i, path in enumerate(paths):
            try:
                canonical, _, info = self._probe_at(i, path, deadline)
                results.append(dict(path=canonical, **info))
            except ap.AgentError as exc:
                results.append(dict(path=path[:ap.MAX_PATH_CHARS] if type(path) is str else "",
                                    error=dict(code=exc.code, message=exc.message)))
        return dict(media=results)
    def create_project(self, name: str) -> dict:
        if (type(name) is not str or not 1 <= len(name) <= ap.MAX_NAME
                or any(unicodedata.category(c).startswith("C") for c in name)):
            raise ap.AgentError("INVALID_ARGUMENT", "Use a name of 1–80 characters without controls.")
        with self.lock:
            if len(self.projects) >= ap.MAX_PROJECTS:
                raise ap.AgentError("LIMIT_EXCEEDED", "Session project limit reached.")
            pid = uuid.uuid4().hex
            slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60] or "project"
            dirname = slug + "-" + pid
            fd = ap.create_project_dir(self.policy, dirname)
            self.projects[pid] = Project(name, slug, dirname, fd)
            return dict(project_id=pid, name=name, revision=0, saved_revision=None)
    def import_media(self, project_id: str, paths: list[str]) -> dict:
        with self.lock:
            p = self._project(project_id)
            known = {identity[:2]: identity for identity in p.identities.values()}
        self._paths(paths)
        deadline = time.monotonic() + ap.PROBE_BUDGET
        probed = [self._probe_at(i, path, deadline, known) for i, path in enumerate(paths)]
        with self.lock:
            p = self._project(project_id)
            assets, identities, results = list(p.state.assets), dict(p.identities), []
            by_identity = {identity[:2]: aid for aid, identity in identities.items()}
            for path, identity, info in probed:
                aid = by_identity.get(identity[:2])
                if aid is not None and identities[aid] != identity:
                    raise ap.AgentError("ASSET_CHANGED", "Previously imported inode changed; create a fresh project.")
                if aid is None:
                    asset = MediaAsset(Path(path), info["duration_s"], info["width"], info["height"],
                                       info["fps"], info["has_audio"])
                    asset.id = uuid.uuid4().hex
                    assets.append(asset)
                    identities[asset.id] = identity
                    by_identity[identity[:2]] = asset.id
                    aid = asset.id
                asset = next(a for a in assets if a.id == aid)
                if not any(r["asset_id"] == aid for r in results):
                    results.append(_asset(asset))
            if len(assets) > ap.MAX_ASSETS:
                raise ap.AgentError("LIMIT_EXCEEDED", "Project asset limit reached.")
            added = len(assets) - len(p.state.assets)
            candidate = _validated(replace(p.state, assets=assets))
            p.state, p.identities = candidate, identities
            p.revision += bool(added)
            return dict(assets=results, added=added, revision=p.revision)
    def set_timeline(self, project_id: str, placement: str, clips: list[dict]) -> dict:
        if placement not in ("sequential", "explicit") or type(clips) is not list:
            raise ap.AgentError("INVALID_ARGUMENT", "Use sequential or explicit placement and a clip list.")
        if len(clips) > ap.MAX_CLIPS:
            raise ap.AgentError("LIMIT_EXCEEDED", "Timeline clip limit reached.")
        with self.lock:
            p = self._project(project_id)
            by_id = {a.id: a for a in p.state.assets}
            candidate, cursor = [], 0.0
            for i, item in enumerate(clips):
                with _context(f"clips[{i}]"):
                    if type(item) is not dict or set(item) - {"asset_id", "src_start_s", "src_end_s",
                            "speed", "timeline_start_s", "mute", "volume"} or not {"asset_id", "src_start_s", "src_end_s"} <= set(item):
                        raise ap.AgentError("INVALID_ARGUMENT", "Clip has invalid fields: " + (repr(sorted(set(item) ^ {"asset_id", "src_start_s", "src_end_s", "speed", "timeline_start_s", "mute", "volume"}))[:100] if type(item) is dict else "expected object"))
                    asset = by_id.get(item["asset_id"]) if type(item["asset_id"]) is str else None
                    if asset is None:
                        raise ap.AgentError("UNKNOWN_ASSET", "unknown asset_id")
                    if asset.kind != "video":
                        raise ap.AgentError("ASSET_NOT_VIDEO", "Only video assets may be placed.")
                    start, end = _number(item["src_start_s"], high=2592000, label="src_start_s"), _number(item["src_end_s"], high=2592000, label="src_end_s")
                    try:
                        _check_source_range("Clip", start, end, asset.duration)
                        if end <= start:
                            raise ProjectError("Empty source range")
                    except ProjectError:
                        raise ap.AgentError("INVALID_TIMELINE", f"src_end_s {end:g} exceeds asset duration {asset.duration:g} s" if end > asset.duration else f"src_start_s {start:g} must be less than src_end_s {end:g}.") from None
                    speed = _number(item.get("speed", 1.0), MIN_CLIP_SPEED, MAX_CLIP_SPEED, "speed")
                    volume = _number(item.get("volume", 1.0), MIN_ITEM_VOLUME, MAX_ITEM_VOLUME, "volume")
                    mute = item.get("mute", False)
                    if type(mute) is not bool:
                        raise ap.AgentError("INVALID_ARGUMENT", "mute must be a boolean.")
                    if (placement == "sequential" and "timeline_start_s" in item
                            or placement == "explicit" and "timeline_start_s" not in item):
                        raise ap.AgentError("INVALID_TIMELINE", "Placement does not match timeline_start_s.")
                    position = cursor if placement == "sequential" else _number(item["timeline_start_s"], label="timeline_start_s")
                    c = Clip(asset, timeline_start=position, src_start=start, src_end=end,
                             speed=speed, muted=mute, audio_volume=volume)
                    cursor = c.timeline_end
                    if cursor > ap.MAX_TIMELINE:
                        raise ap.AgentError("INVALID_TIMELINE", "Timeline exceeds 86400 seconds.")
                    candidate.append(c)
            indices = {id(c): i for i, c in enumerate(candidate)}
            candidate.sort(key=lambda c: c.timeline_start)
            if any(a.timeline_end > b.timeline_start for a, b in zip(candidate, candidate[1:])):
                a, b = next((a, b) for a, b in zip(candidate, candidate[1:]) if a.timeline_end > b.timeline_start)
                raise ap.AgentError("INVALID_TIMELINE", f"clips[{indices[id(b)]}] starts at {b.timeline_start:g} s but clips[{indices[id(a)]}] ends at {a.timeline_end:g} s")
            state = _validated(replace(p.state, clips=candidate))
            p.state = state
            p.revision += 1
            return self.get_project(project_id)
    def set_audio(self, project_id: str, items: list[dict]) -> dict:
        if type(items) is not list:
            raise ap.AgentError("INVALID_ARGUMENT", "Supply an audio item list; [] clears added audio.")
        if len(items) > ap.MAX_AUDIO_ITEMS:
            raise ap.AgentError("LIMIT_EXCEEDED", "Supply at most 50 audio items.")
        with self.lock:
            self._project(project_id)
        audios, identities = [], {}
        deadline = time.monotonic() + ap.PROBE_BUDGET
        for i, item in enumerate(items):
            audio_id = uuid.uuid4().hex
            with _context(f"items[{i}] audio_id {audio_id}"):
                if (type(item) is not dict or "path" not in item or set(item) - {
                        "path", "offset_s", "src_start_s", "src_end_s", "lane", "volume", "mute"}):
                    raise ap.AgentError("INVALID_ARGUMENT", "Audio item requires path and only documented fields.")
                offset = _number(item.get("offset_s", 0), label="offset_s")
                start = _number(item.get("src_start_s", 0), label="src_start_s")
                volume = _number(item.get("volume", 1), MIN_ITEM_VOLUME, MAX_ITEM_VOLUME, "volume")
                lane, mute = item.get("lane", 1), item.get("mute", False)
                if type(lane) is not int or not 0 <= lane <= ap.MAX_AUDIO_LANE:
                    raise ap.AgentError("INVALID_ARGUMENT", "lane must be an integer from 0 to 63.")
                if type(mute) is not bool:
                    raise ap.AgentError("INVALID_ARGUMENT", "mute must be a boolean.")
                path, identity, info = self._probe(item["path"], deadline, kind="audio")
                end = _number(item.get("src_end_s", info["duration_s"]), label="src_end_s")
                try:
                    _check_source_range("Added audio", start, end, info["duration_s"])
                    if end <= start:
                        raise ProjectError("Empty source range")
                except ProjectError:
                    raise ap.AgentError("INVALID_TIMELINE", "Source trim must be nonempty and within the probed audio duration.") from None
                audio = AddedAudio(Path(path), duration=info["duration_s"], offset=offset,
                    src_start=start, src_end=end, lane=lane, volume=volume, muted=mute, id=audio_id)
                if audio.timeline_end > ap.MAX_TIMELINE:
                    raise ap.AgentError("INVALID_TIMELINE", "Audio timeline exceeds 86400 seconds.")
                audios.append(audio)
                identities[audio_id] = identity
        with self.lock:
            p = self._project(project_id)
            candidate = _validated(replace(p.state, added_audios=audios))
            p.state, p.audio_identities = candidate, identities
            p.revision += 1
            return self.get_project(project_id)
    def set_audio_mix(self, project_id: str, original_gain=_UNSET, added_gain=_UNSET,
                      replace_original=_UNSET) -> dict:
        changes = {}
        for name, value in (("original_gain", original_gain), ("added_gain", added_gain)):
            if value is not _UNSET:
                gain = _number(value, MIN_MIX_GAIN, MAX_MIX_GAIN, name)
                if round(gain, MIX_GAIN_DECIMALS) != gain:
                    raise ap.AgentError("INVALID_ARGUMENT", f"{name} permits at most {MIX_GAIN_DECIMALS} decimals; do not round implicitly.")
                changes[name] = gain
        if replace_original is not _UNSET:
            if type(replace_original) is not bool:
                raise ap.AgentError("INVALID_ARGUMENT", "replace_original must be a boolean.")
            changes["replace_added_audio"] = replace_original
        if not changes:
            raise ap.AgentError("INVALID_ARGUMENT", "Supply at least one mix field; omitted fields retain their values.")
        with self.lock:
            p = self._project(project_id)
            p.state = _validated(replace(p.state, **changes))
            p.revision += 1
            return self.get_project(project_id)
    def get_project(self, project_id: str) -> dict:
        with self.lock:
            p = self._project(project_id)
            clips, warnings, cursor = [], [], 0.0
            for i, c in enumerate(p.state.clips):
                if c.timeline_start > cursor and len(warnings) < ap.MAX_WARNINGS:
                    warnings.append(f"Gap from {cursor:g} to {c.timeline_start:g} seconds.")
                if not c.asset.has_audio and (c.audio_volume != 1 or c.muted) and len(warnings) < ap.MAX_WARNINGS:
                    warnings.append(f"clips[{i}]: source has no audio; volume/mute have no effect.")
                cursor = c.timeline_end
                clips.append(dict(asset_id=c.asset.id, src_start_s=c.src_start, src_end_s=c.src_end,
                    speed=c.speed, volume=c.audio_volume, mute=c.muted,
                    timeline_start_s=c.timeline_start, timeline_end_s=c.timeline_end))
            audio = []
            for i, a in enumerate(p.state.added_audios):
                if len(warnings) < ap.MAX_WARNINGS:
                    if a.offset >= cursor:
                        warnings.append(f"audio[{i}] audio_id {a.id}: offset at/after timeline end; inaudible; saving will be refused until moved earlier or the timeline is lengthened.")
                    elif a.timeline_end > cursor:
                        warnings.append(f"audio[{i}] audio_id {a.id}: truncated at timeline end.")
                audio.append(dict(audio_id=a.id, path=str(a.path), duration_s=a.duration,
                    offset_s=a.offset, src_start_s=a.src_start, src_end_s=a.src_end,
                    lane=a.lane, volume=a.volume, mute=a.muted, timeline_end_s=a.timeline_end))
            saved = p.saved or {}
            return dict(name=p.name, revision=p.revision, saved_revision=saved.get("revision"),
                saved_path=saved.get("path"), saved_sha256=saved.get("sha256"),
                saved_is_current=saved.get("revision") == p.revision, assets=[_asset(a) for a in p.state.assets],
                clips=clips, audio=audio, mix=dict(original_gain=p.state.original_gain,
                    added_gain=p.state.added_gain, replace_original=p.state.replace_added_audio),
                duration_s=cursor, warnings=warnings)
    def _verify_project(self, p: Project) -> None:
        """Shared admission/save checks, including the digest of a current save."""
        try:
            st = os.stat(p.dirname, dir_fd=self.policy.output_fd, follow_symlinks=False)
            if not stat.S_ISDIR(st.st_mode) or ap.file_identity(st)[:2] != ap.file_identity(os.fstat(p.fd))[:2]:
                raise OSError()
        except OSError:
            raise ap.AgentError("OUTPUT_UNSAFE", "Project directory was replaced.") from None
        for a in p.state.assets:
            try:
                with ap.open_authorized_media(self.policy, str(a.path)) as (_, _, identity):
                    if identity != p.identities[a.id]:
                        raise OSError()
            except (ap.AgentError, OSError):
                raise ap.AgentError("ASSET_CHANGED", f"asset_id {a.id}: Media changed or is unavailable; re-import and re-save in a fresh project.") from None
        for i, a in enumerate(p.state.added_audios):
            try:
                with ap.open_authorized_media(self.policy, str(a.path), kind="audio") as (_, _, identity):
                    if identity != p.audio_identities.get(a.id):
                        raise OSError()
            except (ap.AgentError, OSError):
                raise ap.AgentError("ASSET_CHANGED", f"audio[{i}] audio_id {a.id} {str(a.path)[:70]!r}: changed or unavailable; call cove_set_audio again, then save.") from None
        if p.saved and p.saved["revision"] == p.revision:
            final = f"{p.slug}.r{p.revision:04d}.coveproj"
            try:
                fd = os.open(final, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=p.fd)
                with os.fdopen(fd, "rb") as stream:
                    st = os.fstat(stream.fileno())
                    if not stat.S_ISREG(st.st_mode) or st.st_size != p.saved["size_bytes"]:
                        raise OSError()
                    digest = hashlib.sha256(stream.read(p.saved["size_bytes"] + 1)).hexdigest()
                if digest != p.saved["sha256"]:
                    raise OSError()
            except OSError:
                raise ap.AgentError("PROJECT_CHANGED_ON_DISK", "Saved revision changed; it will not be rewritten.") from None
    def save_project(self, project_id: str) -> dict:
        with self.lock:
            p = self._project(project_id)
            if not p.state.clips:
                raise ap.AgentError("EMPTY_TIMELINE", "Place at least one clip before saving.")
            timeline_end = max(c.timeline_end for c in p.state.clips)
            for i, a in enumerate(p.state.added_audios):
                if a.offset >= timeline_end:
                    raise ap.AgentError("INVALID_TIMELINE", f"audio[{i}] audio_id {a.id}: offset {a.offset:g} s starts at/after the end of the timeline ({timeline_end:g} s); move it earlier or lengthen the timeline with cove_set_timeline, then save again.")
            self._verify_project(p)
            final = f"{p.slug}.r{p.revision:04d}.coveproj"
            if p.saved and p.saved["revision"] == p.revision:
                return dict(p.saved)
            digest, size = ap.publish(p.fd, final, serialize_project(p.state))
            p.saved = dict(path=str(Path(self.policy.output_root) / p.dirname / final), revision=p.revision,
                           sha256=digest, size_bytes=size)
            return dict(p.saved)
