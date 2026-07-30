#!/usr/bin/env python3
"""
Smoke-test every supported export format.

Usage:
    PYTHONPATH=src python scripts/smoke-export-formats.py

Creates tiny fixtures, runs ExportWorker for each supported format, and reports
pass/fail per format. Exits 0 if all pass, 1 if any fail.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# Qt requires a QCoreApplication for QObject/Signal to function.
# We also need DirectConnection everywhere so signals emitted from the
# ExportWorker thread fire immediately without a running event loop.
from PySide6.QtCore import QCoreApplication, Qt
_qapp = QCoreApplication.instance() or QCoreApplication(sys.argv[:1])

from cove_video_editor import ffmpeg_utils as ff
from cove_video_editor.clip import Clip, MediaAsset
from cove_video_editor.exporter import AudioTrack, ExportJob, ExportWorker

GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
RESET = "\033[0m"

# ---------------------------------------------------------------------------
# Fixture generation
# ---------------------------------------------------------------------------

def _run(cmd: list[str], desc: str) -> None:
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"fixture '{desc}' failed:\n{r.stderr.decode()}")


def make_fixtures(d: Path) -> dict[str, Path]:
    """Return dict of fixture name → path. All clips are ~3 seconds."""
    ffmpeg = ff.require_ffmpeg()
    out: dict[str, Path] = {}

    # Short color video with audio (3s, 640x360, 30fps, stereo)
    video_av = d / "video_av.mp4"
    _run(
        [
            ffmpeg, "-y", "-nostdin",
            "-f", "lavfi", "-i", "color=c=blue:s=640x360:d=3:r=30",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "40",
            "-c:a", "aac", "-b:a", "64k",
            str(video_av),
        ],
        "video+audio",
    )
    out["video_av"] = video_av

    # Short video WITHOUT audio (3s)
    video_no_audio = d / "video_no_audio.mp4"
    _run(
        [
            ffmpeg, "-y", "-nostdin",
            "-f", "lavfi", "-i", "color=c=red:s=640x360:d=3:r=30",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "40",
            "-an",
            str(video_no_audio),
        ],
        "video-no-audio",
    )
    out["video_no_audio"] = video_no_audio

    # Still image (JPEG)
    still_jpg = d / "still.jpg"
    _run(
        [
            ffmpeg, "-y", "-nostdin",
            "-f", "lavfi", "-i", "color=c=green:s=640x360",
            "-frames:v", "1", "-q:v", "5",
            str(still_jpg),
        ],
        "still-jpg",
    )
    out["still_jpg"] = still_jpg

    # Still image (WebP)
    still_webp = d / "still.webp"
    _run(
        [
            ffmpeg, "-y", "-nostdin",
            "-f", "lavfi", "-i", "color=c=purple:s=640x360",
            "-frames:v", "1",
            str(still_webp),
        ],
        "still-webp",
    )
    out["still_webp"] = still_webp

    # Mixed-resolution pair: 1920x1080 primary + 1366x768 secondary.
    # Used to prove concat succeeds when sources differ in size/SAR.
    video_1080 = d / "video_1080.mp4"
    _run(
        [
            ffmpeg, "-y", "-nostdin",
            "-f", "lavfi", "-i", "color=c=blue:s=1920x1080:d=2:r=30",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "40",
            "-c:a", "aac", "-b:a", "64k",
            str(video_1080),
        ],
        "video-1920x1080",
    )
    out["video_1080"] = video_1080

    video_768 = d / "video_768.mp4"
    _run(
        [
            ffmpeg, "-y", "-nostdin",
            "-f", "lavfi", "-i", "color=c=orange:s=1366x768:d=2:r=30",
            "-f", "lavfi", "-i", "sine=frequency=660:duration=2",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "40",
            "-c:a", "aac", "-b:a", "64k",
            str(video_768),
        ],
        "video-1366x768",
    )
    out["video_768"] = video_768

    # Extra audio track (WAV, 3s stereo)
    extra_audio = d / "extra.wav"
    _run(
        [
            ffmpeg, "-y", "-nostdin",
            "-f", "lavfi", "-i", "sine=frequency=880:duration=3",
            "-ar", "48000",
            str(extra_audio),
        ],
        "extra-audio",
    )
    out["extra_audio"] = extra_audio

    return out


# ---------------------------------------------------------------------------
# Asset helpers
# ---------------------------------------------------------------------------

def _asset_from_path(p: Path, kind: str = "video") -> MediaAsset:
    if kind == "image":
        return MediaAsset(
            path=p, duration=3.0, width=640, height=360, fps=0.0,
            has_audio=False, kind="image",
        )
    info = ff.probe(p)
    return MediaAsset(
        path=p, duration=info.duration, width=info.width, height=info.height,
        fps=info.fps, has_audio=info.has_audio, kind="video",
    )


def _make_clip(asset: MediaAsset, start: float = 0.0, dur: float | None = None) -> Clip:
    d = dur if dur is not None else asset.duration
    c = Clip(asset, timeline_start=start)
    c.src_start = 0.0
    c.src_end = min(d, asset.duration) if asset.kind == "video" else d
    return c


# ---------------------------------------------------------------------------
# Output validation
# ---------------------------------------------------------------------------

def _probe_streams(path: Path) -> tuple[list[dict], float]:
    """Return (streams, duration). Uses -count_packets to catch zero-packet exports."""
    out = subprocess.check_output(
        [ff.require_ffprobe(), "-v", "error", "-count_packets",
         "-show_entries", "stream=codec_type,nb_read_packets:format=duration",
         "-of", "json", str(path)],
        text=True,
    )
    data = json.loads(out)
    duration = float(data.get("format", {}).get("duration") or 0)
    return data.get("streams", []), duration


MIXED_RES_LABEL = "mixed-resolution timeline"


def _probe_video_props(path: Path) -> dict:
    """Return codec_name/width/height/sample_aspect_ratio/pix_fmt of stream v:0."""
    out = subprocess.check_output(
        [ff.require_ffprobe(), "-v", "error", "-select_streams", "v:0",
         "-show_entries",
         "stream=codec_name,width,height,sample_aspect_ratio,pix_fmt:format=duration",
         "-of", "json", str(path)],
        text=True,
    )
    data = json.loads(out)
    streams = data.get("streams", [])
    props = dict(streams[0]) if streams else {}
    props["duration"] = float(data.get("format", {}).get("duration") or 0)
    return props


def _validate_mixed_resolution(path: Path) -> tuple[str | None, dict]:
    """Assert the mixed-resolution export matches the first-clip target policy."""
    props = _probe_video_props(path)
    if not props.get("codec_name"):
        return "no video stream", props
    if (props.get("width"), props.get("height")) != (1920, 1080):
        return f"expected 1920x1080, got {props.get('width')}x{props.get('height')}", props
    sar = props.get("sample_aspect_ratio") or "1:1"
    if sar not in ("1:1", "N/A"):
        return f"expected SAR 1:1, got {sar}", props
    if props["duration"] <= 0:
        return f"zero duration ({props['duration']:.3f}s)", props
    return None, props


def _validate_output(path: Path, spec: dict) -> str | None:
    """Return an error string, or None if the output looks valid."""
    try:
        streams, duration = _probe_streams(path)
    except Exception as exc:
        return f"ffprobe failed: {exc}"

    if duration <= 0:
        return f"zero/missing duration ({duration:.3f}s)"

    is_audio_only = spec.get("vcodec") is None
    is_gif = spec.get("vcodec") == "gif"

    def has(codec_type: str) -> bool:
        return any(
            s.get("codec_type") == codec_type and int(s.get("nb_read_packets") or 0) > 0
            for s in streams
        )

    if is_audio_only:
        if not has("audio"):
            return "no audio packets in audio-only export"
    elif is_gif:
        if not has("video"):
            return "no video packets in GIF export"
    else:
        if not has("video"):
            return "no video packets"
        if not has("audio"):
            return "no audio packets"
    return None


# ---------------------------------------------------------------------------
# Worker runner (synchronous in a thread)
# ---------------------------------------------------------------------------

def run_export(job: ExportJob, timeout: float = 60.0) -> tuple[bool, str]:
    """Return (success, message). Runs synchronously."""
    log_lines: list[str] = []
    result: dict = {}
    done = threading.Event()

    worker = ExportWorker(job)

    def on_log(msg: str) -> None:
        log_lines.append(msg)

    def on_finished(p: Path) -> None:
        result["ok"] = True
        result["msg"] = f"OK → {p.stat().st_size} bytes"
        done.set()

    def on_failed(msg: str) -> None:
        result["ok"] = False
        result["msg"] = msg
        done.set()

    # DirectConnection so callbacks fire from the worker thread immediately
    # (no event loop running in main thread to process queued signals).
    worker.log.connect(on_log, Qt.DirectConnection)
    worker.finished.connect(on_finished, Qt.DirectConnection)
    worker.failed.connect(on_failed, Qt.DirectConnection)

    t = threading.Thread(target=worker.run, daemon=True)
    t.start()
    if not done.wait(timeout):
        worker.cancel()
        t.join(timeout=10.0)
        cleanup = "" if not t.is_alive() else " (cleanup failed — ffmpeg may still be running)"
        return False, f"TIMEOUT after {timeout}s{cleanup}"

    if not result.get("ok"):
        detail = "\n  ".join(log_lines[-10:]) if log_lines else ""
        return False, result.get("msg", "unknown") + (f"\n  LOG:\n  {detail}" if detail else "")

    spec = ff.EXPORT_FORMATS.get(job.fmt_key, {})
    err = _validate_output(job.output, spec)
    if err:
        return False, f"validation failed: {err}"

    return True, result.get("msg", "ok")


# ---------------------------------------------------------------------------
# Test matrix
# ---------------------------------------------------------------------------

def build_matrix(fixtures: dict[str, Path], out_dir: Path) -> list[tuple[str, ExportJob]]:
    """Return (label, ExportJob) pairs covering all supported scenarios."""
    cases: list[tuple[str, ExportJob]] = []

    video_av_asset = _asset_from_path(fixtures["video_av"])
    video_no_audio_asset = _asset_from_path(fixtures["video_no_audio"])
    still_jpg_asset = _asset_from_path(fixtures["still_jpg"], kind="image")
    still_webp_asset = _asset_from_path(fixtures["still_webp"], kind="image")

    for fmt_key, spec in ff.EXPORT_FORMATS.items():
        ext = spec["ext"]
        is_audio_only = spec["vcodec"] is None

        if is_audio_only:
            # Audio-only: use video+audio clip as source
            clip = _make_clip(video_av_asset)
            cases.append((
                f"{fmt_key} | video+audio clip",
                ExportJob(
                    clips=[clip],
                    output=out_dir / f"video_av.{ext}",
                    fmt_key=fmt_key,
                ),
            ))
            # Audio-only from image-only timeline (should export silence)
            still_clip = _make_clip(still_jpg_asset, dur=3.0)
            cases.append((
                f"{fmt_key} | image-only timeline",
                ExportJob(
                    clips=[still_clip],
                    output=out_dir / f"image_only.{ext}",
                    fmt_key=fmt_key,
                ),
            ))
        else:
            # Video format: test several source types
            for label, asset in [
                ("video+audio clip", video_av_asset),
                ("video no-audio clip", video_no_audio_asset),
                ("still JPEG", still_jpg_asset),
                ("still WebP", still_webp_asset),
            ]:
                slug = label.replace(" ", "_").replace("+", "_").replace("-", "_")
                clip = _make_clip(asset, dur=3.0)
                cases.append((
                    f"{fmt_key} | {label}",
                    ExportJob(
                        clips=[clip],
                        output=out_dir / f"{slug}.{ext}",
                        fmt_key=fmt_key,
                    ),
                ))

            # Gap timeline: two clips with a 0.5s gap
            c1 = _make_clip(video_av_asset, start=0.0, dur=1.5)
            c2 = _make_clip(video_av_asset, start=2.0, dur=1.5)
            cases.append((
                f"{fmt_key} | gap timeline",
                ExportJob(
                    clips=[c1, c2],
                    output=out_dir / f"gap.{ext}",
                    fmt_key=fmt_key,
                ),
            ))

            # Mixed-resolution timeline: 1920x1080 followed by 1366x768.
            # Only exercised for MP4/H.264 to keep harness time down.
            if fmt_key == "MP4 (H.264 + AAC)":
                m1 = _make_clip(_asset_from_path(fixtures["video_1080"]), start=0.0, dur=2.0)
                m2 = _make_clip(_asset_from_path(fixtures["video_768"]), start=2.0, dur=2.0)
                cases.append((
                    f"{fmt_key} | {MIXED_RES_LABEL}",
                    ExportJob(
                        clips=[m1, m2],
                        output=out_dir / f"mixed_resolution.{ext}",
                        fmt_key=fmt_key,
                    ),
                ))

            # Added-audio track
            extra_track = AudioTrack(
                path=fixtures["extra_audio"],
                offset=0.0,
                duration=3.0,
            )
            clip_a = _make_clip(video_av_asset, dur=3.0)
            cases.append((
                f"{fmt_key} | added-audio track",
                ExportJob(
                    clips=[clip_a],
                    output=out_dir / f"added_audio.{ext}",
                    fmt_key=fmt_key,
                    audio_tracks=[extra_track],
                ),
            ))

    return cases


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    with tempfile.TemporaryDirectory(prefix="cove-smoke-") as tmp:
        tmp_path = Path(tmp)
        fixture_dir = tmp_path / "fixtures"
        fixture_dir.mkdir()
        out_dir = tmp_path / "out"
        out_dir.mkdir()

        print("Building fixtures …")
        try:
            fixtures = make_fixtures(fixture_dir)
        except RuntimeError as e:
            print(f"{RED}ERROR building fixtures: {e}{RESET}")
            return 1
        print(f"  {len(fixtures)} fixtures ready.\n")

        cases = build_matrix(fixtures, out_dir)
        print(f"Running {len(cases)} export cases …\n")

        passed = 0
        failed = 0
        skipped = 0
        failures: list[tuple[str, str]] = []

        for label, job in cases:
            # Use a per-format output dir to avoid name collisions
            fmt_slug = job.fmt_key.replace(" ", "_").replace("(", "").replace(")", "").replace("+", "_")
            job_out = out_dir / fmt_slug
            job_out.mkdir(exist_ok=True)
            job.output = job_out / job.output.name

            t0 = time.monotonic()
            ok, msg = run_export(job, timeout=90.0)
            elapsed = time.monotonic() - t0

            if ok and MIXED_RES_LABEL in label:
                err, props = _validate_mixed_resolution(job.output)
                # Unique destination so repeated runs never clobber an
                # earlier artifact (or an unrelated file) in the temp dir.
                kept_dir = Path(tempfile.mkdtemp(prefix="cove-smoke-mixed-"))
                kept = kept_dir / "mixed-resolution.mp4"
                shutil.copy2(job.output, kept)
                print(f"           mixed-resolution artifact: {kept}")
                print(
                    "           ffprobe v:0 "
                    f"codec_name={props.get('codec_name')} "
                    f"width={props.get('width')} height={props.get('height')} "
                    f"sample_aspect_ratio={props.get('sample_aspect_ratio')} "
                    f"pix_fmt={props.get('pix_fmt')} "
                    f"duration={props.get('duration'):.3f}"
                )
                if err:
                    ok, msg = False, f"mixed-resolution validation failed: {err}"

            if ok:
                print(f"  {GREEN}PASS{RESET}  [{elapsed:5.1f}s]  {label}")
                passed += 1
            else:
                print(f"  {RED}FAIL{RESET}  [{elapsed:5.1f}s]  {label}")
                print(f"           {YELLOW}{msg.splitlines()[0]}{RESET}")
                failed += 1
                failures.append((label, msg))

        print(f"\n{'='*60}")
        print(f"Results: {passed} passed, {failed} failed, {skipped} skipped")
        if failures:
            print(f"\nFailed cases:")
            for label, msg in failures:
                print(f"  {RED}✗{RESET} {label}")
                for line in msg.splitlines():
                    print(f"    {line}")
        print()

        return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
