"""Tab 2E: hardware-accelerated video export (NVIDIA NVENC / AMD AMF).

The whole feature is "pick the best usable video encoder at export time".
Nothing here needs a real GPU: the FFmpeg process is faked with
``subprocess.CompletedProcess``-shaped results so both the "encoder is
listed but cannot initialize" case and the "encoder really works" case are
exercised on any machine.

The Qt pieces follow the repo's existing lightweight pattern - bare
``MainWindow.__new__`` instances driven through unbound methods, plus real
``QComboBox`` widgets on the ``offscreen`` platform plugin.
"""
from __future__ import annotations

import ast
import os
import subprocess
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QComboBox  # noqa: E402

from cove_video_editor import app as app_mod  # noqa: E402
from cove_video_editor import ffmpeg_utils as ff  # noqa: E402
from cove_video_editor.clip import Clip, MediaAsset  # noqa: E402
from cove_video_editor.exporter import (  # noqa: E402
    AudioTrack,
    ExportJob,
    ExportWorker,
    build_export_video_encoder_args,
    resolve_export_video_encoder,
)

_APP: QApplication | None = None


def setUpModule() -> None:
    global _APP
    _APP = QApplication.instance() or QApplication([])


# --- helpers ---------------------------------------------------------------

_LISTING = (
    " V....D h264_nvenc           NVIDIA NVENC H.264 encoder (codec h264)\n"
    " V....D hevc_nvenc           NVIDIA NVENC hevc encoder (codec hevc)\n"
    " V....D h264_amf             AMD AMF H.264 Encoder (codec h264)\n"
    " V....D hevc_amf             AMD AMF HEVC encoder (codec hevc)\n"
)


class _FakeProc:
    """`subprocess.Popen` stand-in with the surface `_run_probe` uses.

    `communicate()` returns a real `(stdout, stderr)` tuple and
    `returncode` is a real int, so production's success path is exercised
    rather than a fake that only models failure.
    """

    def __init__(self, cmd, returncode: int, stdout: str, record: dict,
                 error: BaseException | None = None, on_run=None) -> None:
        self.args = cmd
        self.returncode = returncode
        self._stdout = stdout
        self._record = record
        self._error = error
        self._on_run = on_run
        self.kill_calls = 0

    def communicate(self, timeout=None):  # noqa: ANN001
        # The first bound is the probe's own; a second call is the short
        # reap after a kill.
        self._record.setdefault("timeout", timeout)
        self._record.setdefault("timeouts", []).append(timeout)
        if self._on_run is not None:
            self._on_run()
        if self._error is not None and not self.kill_calls:
            raise self._error
        return (self._stdout, "")

    def kill(self) -> None:
        self.kill_calls += 1
        self.returncode = -9


class _FakeFFmpeg:
    """Stands in for `subprocess.Popen` against the ffmpeg binary."""

    def __init__(
        self,
        *,
        listing: str = _LISTING,
        init_returncode: int = 0,
        list_error: BaseException | None = None,
        init_error: BaseException | None = None,
        on_init=None,
        on_init_spawn=None,
    ) -> None:
        self.listing = listing
        self.init_returncode = init_returncode
        self.list_error = list_error
        self.init_error = init_error
        self.on_init = on_init
        # Fires while the init child is being created, i.e. after the
        # worker's cancellation check and before production can register
        # the process. That is exactly the shutdown race window.
        self.on_init_spawn = on_init_spawn
        self.calls: list[tuple[list[str], dict]] = []
        self.procs: list[_FakeProc] = []

    @property
    def list_calls(self) -> list[tuple[list[str], dict]]:
        return [c for c in self.calls if "-encoders" in c[0]]

    @property
    def init_calls(self) -> list[tuple[list[str], dict]]:
        return [c for c in self.calls if "-encoders" not in c[0]]

    def __call__(self, cmd, **kwargs):  # noqa: ANN001
        record: dict = {}
        self.calls.append((list(cmd), record))
        if "-encoders" in cmd:
            if isinstance(self.list_error, OSError):
                raise self.list_error
            proc = _FakeProc(list(cmd), 0, self.listing, record,
                             error=self.list_error)
        else:
            if isinstance(self.init_error, OSError):
                raise self.init_error
            if self.on_init_spawn is not None:
                self.on_init_spawn()
            proc = _FakeProc(list(cmd), self.init_returncode, "", record,
                             error=self.init_error, on_run=self.on_init)
        self.procs.append(proc)
        return proc


class _HardwareProbeCase(unittest.TestCase):
    """Every probe test starts from a cold cache and no pending abort."""

    def setUp(self) -> None:
        ff.reset_hardware_encoder_cache()
        ff.clear_hardware_probe_abort()
        self.addCleanup(ff.clear_hardware_probe_abort)
        self.addCleanup(ff.reset_hardware_encoder_cache)


def _fake_ffmpeg(fake: _FakeFFmpeg):
    """Patch both the binary resolver and the subprocess entry point."""
    return (
        patch.object(ff, "require_ffmpeg", return_value="/fake/bin/ffmpeg"),
        patch("cove_video_editor.ffmpeg_utils.subprocess.Popen", fake),
    )


class _Patched:
    def __init__(self, fake: _FakeFFmpeg) -> None:
        self._ctxs = _fake_ffmpeg(fake)

    def __enter__(self):
        for c in self._ctxs:
            c.__enter__()
        return self

    def __exit__(self, *exc):
        for c in reversed(self._ctxs):
            c.__exit__(*exc)
        return False


def _val(args: list[str], flag: str) -> str | None:
    for i, tok in enumerate(args[:-1]):
        if tok == flag:
            return args[i + 1]
    return None


def _asset(name: str, *, width: int = 1280, height: int = 720,
           has_audio: bool = True, kind: str = "video") -> MediaAsset:
    return MediaAsset(
        path=Path(name), duration=2.0, width=width, height=height,
        fps=30.0, has_audio=has_audio, kind=kind,
    )


def _clip(name: str = "a.mp4", *, start: float = 0.0,
          width: int = 1280, height: int = 720) -> Clip:
    return Clip(
        asset=_asset(name, width=width, height=height),
        timeline_start=start, src_start=0.0, src_end=2.0,
    )


def _filter_complex(cmd: list[str]) -> str:
    return cmd[cmd.index("-filter_complex") + 1]


def _spec(fmt_key: str) -> dict:
    return ff.EXPORT_FORMATS[fmt_key]


# --- Group A: encoder list + real initialization probe ---------------------


class GroupAEncoderListAndInitProbe(_HardwareProbeCase):
    def test_a1_encoder_absent_from_list_never_runs_an_init_encode(self) -> None:
        fake = _FakeFFmpeg(listing=" V....D libx264   H.264 encoder\n")
        with _Patched(fake):
            self.assertFalse(ff.nvenc_available("h264_nvenc"))
        self.assertEqual(len(fake.init_calls), 0)
        self.assertEqual(len(fake.list_calls), 1)

    def test_a2_listed_encoder_that_initializes_is_available(self) -> None:
        fake = _FakeFFmpeg(init_returncode=0)
        with _Patched(fake):
            self.assertTrue(ff.nvenc_available("h264_nvenc"))
        self.assertEqual(len(fake.init_calls), 1)
        init_cmd, init_kwargs = fake.init_calls[0]
        self.assertEqual(_val(init_cmd, "-c:v"), "h264_nvenc")
        self.assertEqual(_val(init_cmd, "-f"), "lavfi")
        self.assertIn("null", init_cmd)
        self.assertEqual(init_kwargs["timeout"], ff.ENCODER_PROBE_TIMEOUT)
        self.assertEqual(fake.list_calls[0][1]["timeout"], ff.ENCODER_LIST_TIMEOUT)

    def test_a2b_probe_timeouts_are_bounded_and_small(self) -> None:
        self.assertLessEqual(ff.ENCODER_LIST_TIMEOUT, 15)
        self.assertLessEqual(ff.ENCODER_PROBE_TIMEOUT, 30)
        self.assertGreater(ff.ENCODER_LIST_TIMEOUT, 0)
        self.assertGreater(ff.ENCODER_PROBE_TIMEOUT, 0)

    def test_a2c_probe_writes_no_user_file_and_reads_no_project_media(self) -> None:
        fake = _FakeFFmpeg()
        with _Patched(fake):
            ff.nvenc_available("h264_nvenc")
        init_cmd = fake.init_calls[0][0]
        # Synthetic lavfi source, null muxer, os.devnull sink.
        self.assertTrue(any(tok.startswith("color=") for tok in init_cmd))
        self.assertEqual(init_cmd[-1], os.devnull)
        self.assertEqual(init_cmd[init_cmd.index("null") - 1], "-f")

    def test_a3_listed_encoder_that_fails_to_initialize_is_unavailable(self) -> None:
        fake = _FakeFFmpeg(init_returncode=1)
        with _Patched(fake):
            self.assertFalse(ff.nvenc_available("h264_nvenc"))
        self.assertEqual(len(fake.init_calls), 1)

    def test_a3b_listed_amf_that_fails_to_initialize_is_unavailable(self) -> None:
        fake = _FakeFFmpeg(init_returncode=1)
        with _Patched(fake):
            self.assertFalse(ff.amf_available("h264_amf"))
        self.assertEqual(_val(fake.init_calls[0][0], "-c:v"), "h264_amf")

    def test_a4_init_probe_timeout_is_unavailable_not_an_exception(self) -> None:
        fake = _FakeFFmpeg(
            init_error=subprocess.TimeoutExpired("ffmpeg", ff.ENCODER_PROBE_TIMEOUT)
        )
        with _Patched(fake):
            self.assertFalse(ff.nvenc_available("h264_nvenc"))
            self.assertFalse(ff.amf_available("hevc_amf"))
        self.assertEqual(fake.init_calls[0][1]["timeout"], ff.ENCODER_PROBE_TIMEOUT)

    def test_a4b_encoder_listing_timeout_is_unavailable(self) -> None:
        fake = _FakeFFmpeg(
            list_error=subprocess.TimeoutExpired("ffmpeg", ff.ENCODER_LIST_TIMEOUT)
        )
        with _Patched(fake):
            self.assertFalse(ff.nvenc_available("h264_nvenc"))
        self.assertEqual(len(fake.init_calls), 0)

    def test_a5_missing_ffmpeg_binary_is_unavailable_with_no_process(self) -> None:
        fake = _FakeFFmpeg()
        with patch.object(ff, "require_ffmpeg",
                          side_effect=ff.FFmpegMissingError("ffmpeg not found")), \
                patch("cove_video_editor.ffmpeg_utils.subprocess.Popen", fake):
            self.assertFalse(ff.nvenc_available("h264_nvenc"))
            self.assertFalse(ff.amf_available("h264_amf"))
        self.assertEqual(fake.calls, [])

    def test_a5b_process_launch_failure_is_unavailable(self) -> None:
        fake = _FakeFFmpeg(init_error=OSError("exec format error"))
        with _Patched(fake):
            self.assertFalse(ff.nvenc_available("hevc_nvenc"))

    def test_a5c_listing_launch_failure_is_unavailable(self) -> None:
        fake = _FakeFFmpeg(list_error=OSError("no such file"))
        with _Patched(fake):
            self.assertFalse(ff.amf_available("hevc_amf"))
        self.assertEqual(len(fake.init_calls), 0)

    def test_a6_every_hardware_encoder_cove_selects_is_probeable(self) -> None:
        self.assertEqual(
            set(ff.HARDWARE_ENCODERS),
            {"h264_nvenc", "hevc_nvenc", "h264_amf", "hevc_amf"},
        )
        hw_codecs = {
            spec[k] for spec in ff.EXPORT_FORMATS.values()
            for k in ("nvenc_codec", "amf_codec") if spec.get(k)
        }
        self.assertTrue(hw_codecs.issubset(set(ff.HARDWARE_ENCODERS)))

    def test_a7_the_probe_uses_the_production_encoder_arguments(self) -> None:
        # A build can initialize an encoder yet reject one of Cove's rate
        # control options; probing with anything weaker hides that.
        fake = _FakeFFmpeg(init_returncode=0)
        with _Patched(fake):
            ff.nvenc_available("h264_nvenc")
        init_cmd = fake.init_calls[0][0]
        for token in build_export_video_encoder_args("h264_nvenc"):
            self.assertIn(token, init_cmd)
        self.assertEqual(_val(init_cmd, "-rc"), "vbr")
        self.assertEqual(_val(init_cmd, "-cq"), "22")

    def test_a7b_the_amf_probe_carries_the_amf_rate_control(self) -> None:
        fake = _FakeFFmpeg(init_returncode=0)
        with _Patched(fake):
            ff.amf_available("hevc_amf")
        init_cmd = fake.init_calls[0][0]
        self.assertEqual(_val(init_cmd, "-rc"), "cqp")
        self.assertEqual(_val(init_cmd, "-qp_i"), "27")
        self.assertNotIn("-qp", init_cmd)


# --- Group B: per-process cache -------------------------------------------


class GroupBProbeCache(_HardwareProbeCase):
    def test_b1_first_call_runs_the_listing_and_the_init_probe(self) -> None:
        fake = _FakeFFmpeg()
        with _Patched(fake):
            self.assertTrue(ff.nvenc_available("h264_nvenc"))
        self.assertEqual(len(fake.list_calls), 1)
        self.assertEqual(len(fake.init_calls), 1)

    def test_b2_repeat_calls_are_cached_and_spawn_nothing(self) -> None:
        fake = _FakeFFmpeg()
        with _Patched(fake):
            self.assertTrue(ff.nvenc_available("h264_nvenc"))
            before = len(fake.calls)
            for _ in range(5):
                self.assertTrue(ff.nvenc_available("h264_nvenc"))
            self.assertEqual(len(fake.calls), before)

    def test_b3_negative_results_are_cached_too(self) -> None:
        fake = _FakeFFmpeg(init_returncode=1)
        with _Patched(fake):
            self.assertFalse(ff.nvenc_available("h264_nvenc"))
            self.assertFalse(ff.nvenc_available("h264_nvenc"))
            self.assertFalse(ff.nvenc_available("h264_nvenc"))
        self.assertEqual(len(fake.init_calls), 1)

    def test_b4_nvenc_and_amf_results_are_independent(self) -> None:
        def _rc(cmd) -> int:
            return 0 if "h264_nvenc" in cmd else 1

        class _Mixed(_FakeFFmpeg):
            def __call__(self, cmd, **kwargs):  # noqa: ANN001
                record: dict = {}
                self.calls.append((list(cmd), record))
                listed = "-encoders" in cmd
                proc = _FakeProc(list(cmd), 0 if listed else _rc(cmd),
                                 self.listing if listed else "", record)
                self.procs.append(proc)
                return proc

        fake = _Mixed()
        with _Patched(fake):
            self.assertTrue(ff.nvenc_available("h264_nvenc"))
            self.assertFalse(ff.amf_available("h264_amf"))
            # And back again - neither poisoned the other.
            self.assertTrue(ff.nvenc_available("h264_nvenc"))
            self.assertFalse(ff.amf_available("h264_amf"))
        self.assertEqual(len(fake.init_calls), 2)

    def test_b4b_encoder_listing_is_shared_across_probes(self) -> None:
        fake = _FakeFFmpeg()
        with _Patched(fake):
            ff.nvenc_available("h264_nvenc")
            ff.amf_available("h264_amf")
        self.assertEqual(len(fake.list_calls), 1)

    def test_b5_concurrent_requests_probe_once_and_the_second_waits(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        def _hold() -> None:
            entered.set()
            self.assertTrue(release.wait(timeout=10), "probe was never released")

        fake = _FakeFFmpeg(on_init=_hold)
        results: dict[str, bool] = {}

        def _ask(tag: str) -> None:
            results[tag] = ff.nvenc_available("h264_nvenc")

        with _Patched(fake):
            first = threading.Thread(target=_ask, args=("a",))
            first.start()
            self.assertTrue(entered.wait(timeout=10), "first probe never started")
            second = threading.Thread(target=_ask, args=("b",))
            second.start()
            # The first probe still holds the per-encoder lock and nothing is
            # cached yet, so the second caller cannot have finished.
            second.join(0.25)
            self.assertTrue(second.is_alive())
            release.set()
            first.join(timeout=10)
            second.join(timeout=10)

        self.assertEqual(results, {"a": True, "b": True})
        self.assertEqual(len(fake.init_calls), 1)

    def test_b7_a_timed_out_probe_kills_its_child(self) -> None:
        fake = _FakeFFmpeg(
            init_error=subprocess.TimeoutExpired("ffmpeg", ff.ENCODER_PROBE_TIMEOUT)
        )
        with _Patched(fake):
            self.assertFalse(ff.nvenc_available("h264_nvenc"))
        init_proc = fake.procs[-1]
        self.assertEqual(init_proc.kill_calls, 1)
        # And nothing is left registered for a later abort to touch.
        self.assertEqual(len(ff._active_probe_procs), 0)

    def test_b8_abort_kills_the_probe_that_is_running_right_now(self) -> None:
        entered = threading.Event()
        released = threading.Event()

        def _hold() -> None:
            entered.set()
            self.assertTrue(released.wait(timeout=10), "probe never released")

        fake = _FakeFFmpeg(on_init=_hold)
        with _Patched(fake):
            worker = threading.Thread(
                target=lambda: ff.nvenc_available("h264_nvenc"))
            worker.start()
            self.assertTrue(entered.wait(timeout=10), "probe never started")
            # The live child is registered, so a shutdown can end it.
            self.assertEqual(len(ff._active_probe_procs), 1)
            ff.abort_hardware_probes()
            self.assertEqual(fake.procs[-1].kill_calls, 1)
            released.set()
            worker.join(timeout=10)
        self.assertEqual(len(ff._active_probe_procs), 0)

    def test_b10_abort_between_the_cancel_check_and_registration_still_kills(
        self,
    ) -> None:
        """The shutdown race: cancellation lands after the worker checked
        `_cancelled` and after ffmpeg was spawned, but before production
        could register the child. Without coordination the abort sees an
        empty registry, kills nothing, and the probe outlives shutdown."""
        fake = _FakeFFmpeg(init_returncode=0,
                           on_init_spawn=ff.abort_hardware_probes)
        with _Patched(fake):
            self.assertFalse(ff.nvenc_available("h264_nvenc"))
        init_proc = fake.procs[-1]
        self.assertEqual(init_proc.kill_calls, 1)
        self.assertEqual(len(ff._active_probe_procs), 0)

    def test_b11_no_further_probe_stage_starts_once_aborted(self) -> None:
        fake = _FakeFFmpeg(init_returncode=0)
        with _Patched(fake):
            ff.abort_hardware_probes()
            self.assertFalse(ff.nvenc_available("h264_nvenc"))
            self.assertFalse(ff.amf_available("h264_amf"))
        # Not even the encoder listing is worth spawning during shutdown.
        self.assertEqual(fake.calls, [])

    def test_b12_an_aborted_result_is_not_cached(self) -> None:
        """A shutdown artifact must not poison the next window's probes."""
        fake = _FakeFFmpeg(init_returncode=0,
                           on_init_spawn=ff.abort_hardware_probes)
        with _Patched(fake):
            self.assertFalse(ff.nvenc_available("h264_nvenc"))
            self.assertNotIn("h264_nvenc", ff._encoder_cache)

        fresh = _FakeFFmpeg(init_returncode=0)
        with _Patched(fresh):
            ff.clear_hardware_probe_abort()
            self.assertTrue(ff.nvenc_available("h264_nvenc"))
        self.assertEqual(len(fresh.init_calls), 1)

    def test_b13_clearing_the_abort_lets_probing_resume(self) -> None:
        fake = _FakeFFmpeg(init_returncode=0)
        with _Patched(fake):
            ff.abort_hardware_probes()
            self.assertFalse(ff.nvenc_available("h264_nvenc"))
            self.assertEqual(fake.calls, [])
            ff.clear_hardware_probe_abort()
            self.assertTrue(ff.nvenc_available("h264_nvenc"))
        self.assertEqual(len(fake.init_calls), 1)

    def test_b14_a_new_probe_session_clears_a_stale_abort(self) -> None:
        import inspect
        src = inspect.getsource(app_mod.MainWindow._start_encoder_probe)
        self.assertIn("clear_hardware_probe_abort", src)

    # --- probe generation ownership ---------------------------------------
    #
    # A probe parked past the shutdown grace can still be running when a new
    # window re-arms probing. Its result belongs to the *aborted* session and
    # must never reach the process-wide cache, in either direction.

    def _stale_probe(self, result: bool):
        """A probe from an older session that finishes after a newer session
        has already re-armed probing."""

        def _probe(encoder, generation=None):  # noqa: ANN001
            ff.abort_hardware_probes()        # the old window closes...
            ff.clear_hardware_probe_abort()   # ...and a new one re-arms
            return result

        return _probe

    def test_b15_a_stale_result_cannot_enter_the_cache_after_rearm(self) -> None:
        with patch.object(ff, "_probe_hardware_encoder", self._stale_probe(True)):
            ff.hardware_encoder_available("h264_nvenc")
        self.assertNotIn("h264_nvenc", ff._encoder_cache)

    def test_b16_a_stale_negative_cannot_disable_working_hardware(self) -> None:
        with patch.object(ff, "_probe_hardware_encoder", self._stale_probe(False)):
            ff.hardware_encoder_available("h264_nvenc")
        self.assertNotIn("h264_nvenc", ff._encoder_cache)

        # The new session probes for itself and gets the true answer.
        fresh = _FakeFFmpeg(init_returncode=0)
        with _Patched(fresh):
            self.assertTrue(ff.nvenc_available("h264_nvenc"))
        self.assertEqual(len(fresh.init_calls), 1)
        self.assertTrue(ff._encoder_cache["h264_nvenc"])

    def test_b17_a_stale_positive_cannot_falsely_enable_hardware(self) -> None:
        with patch.object(ff, "_probe_hardware_encoder", self._stale_probe(True)):
            ff.hardware_encoder_available("h264_amf")
        self.assertNotIn("h264_amf", ff._encoder_cache)

        fresh = _FakeFFmpeg(init_returncode=1)   # AMF really does not work
        with _Patched(fresh):
            self.assertFalse(ff.amf_available("h264_amf"))
        self.assertFalse(ff._encoder_cache["h264_amf"])

    def test_b18_the_new_generation_still_caches_normally(self) -> None:
        ff.abort_hardware_probes()
        ff.clear_hardware_probe_abort()
        fake = _FakeFFmpeg(init_returncode=0)
        with _Patched(fake):
            self.assertTrue(ff.nvenc_available("h264_nvenc"))
            self.assertTrue(ff.nvenc_available("h264_nvenc"))
            self.assertFalse(ff.amf_available("h264_amf") is None)
        # Cached after one probe, exactly as before the generation work.
        self.assertEqual(len(fake.init_calls), 2)
        self.assertIn("h264_nvenc", ff._encoder_cache)

    def test_b19_aborting_a_generation_marks_it_permanently(self) -> None:
        old = ff.current_probe_generation()
        ff.abort_hardware_probes()
        self.assertTrue(ff.hardware_probes_aborted(old))

        ff.clear_hardware_probe_abort()
        new = ff.current_probe_generation()
        self.assertNotEqual(new, old)
        # Re-arming starts a newer generation; it never un-aborts the old one.
        self.assertTrue(ff.hardware_probes_aborted(old))
        self.assertFalse(ff.hardware_probes_aborted(new))
        self.assertFalse(ff.hardware_probes_aborted())

    def test_b19b_rearming_a_clean_generation_is_a_no_op(self) -> None:
        gen = ff.current_probe_generation()
        ff.clear_hardware_probe_abort()
        self.assertEqual(ff.current_probe_generation(), gen)

    def test_b9_a_killed_probe_reports_unavailable(self) -> None:
        fake = _FakeFFmpeg(init_returncode=0, on_init=ff.abort_hardware_probes)
        with _Patched(fake):
            # abort() runs while the child is registered, so it is killed
            # and the non-zero exit means "unavailable", not a crash.
            self.assertFalse(ff.nvenc_available("h264_nvenc"))

    def test_b6_reset_clears_the_cache(self) -> None:
        fake = _FakeFFmpeg()
        with _Patched(fake):
            ff.nvenc_available("h264_nvenc")
            ff.reset_hardware_encoder_cache()
            ff.nvenc_available("h264_nvenc")
        self.assertEqual(len(fake.init_calls), 2)


# --- Group C: preference -> encoder resolution ----------------------------


def _availability(*, nvenc: bool, amf: bool):
    return (
        patch.object(ff, "nvenc_available", return_value=nvenc),
        patch.object(ff, "amf_available", return_value=amf),
    )


class _Avail:
    def __init__(self, *, nvenc: bool, amf: bool) -> None:
        self._ctxs = _availability(nvenc=nvenc, amf=amf)
        self.mocks: list = []

    def __enter__(self):
        self.mocks = [c.__enter__() for c in self._ctxs]
        return self

    def __exit__(self, *exc):
        for c in reversed(self._ctxs):
            c.__exit__(*exc)
        return False


class GroupCEncoderResolver(unittest.TestCase):
    H264 = "MP4 (H.264 + AAC)"
    H265 = "MP4 (H.265 + AAC)"

    def test_c1_default_auto_with_no_hardware_resolves_to_cpu(self) -> None:
        job = ExportJob(clips=[_clip()], output=Path("o.mp4"), fmt_key=self.H264)
        self.assertEqual(job.encoder_pref, "auto")
        with _Avail(nvenc=False, amf=False):
            self.assertEqual(
                resolve_export_video_encoder(_spec(self.H264), job.encoder_pref),
                "libx264",
            )

    def test_c2_auto_uses_nvenc_when_available(self) -> None:
        with _Avail(nvenc=True, amf=False):
            self.assertEqual(
                resolve_export_video_encoder(_spec(self.H264), "auto"), "h264_nvenc")

    def test_c3_auto_falls_through_to_amf_when_nvenc_is_unavailable(self) -> None:
        with _Avail(nvenc=False, amf=True):
            self.assertEqual(
                resolve_export_video_encoder(_spec(self.H264), "auto"), "h264_amf")

    def test_c4_auto_prefers_nvenc_when_both_are_available(self) -> None:
        with _Avail(nvenc=True, amf=True):
            self.assertEqual(
                resolve_export_video_encoder(_spec(self.H264), "auto"), "h264_nvenc")
            self.assertEqual(
                resolve_export_video_encoder(_spec(self.H265), "auto"), "hevc_nvenc")

    def test_c5_explicit_cpu_never_probes_and_never_uses_hardware(self) -> None:
        with _Avail(nvenc=True, amf=True) as av:
            self.assertEqual(
                resolve_export_video_encoder(_spec(self.H264), "cpu"), "libx264")
            nvenc_mock, amf_mock = av.mocks
            nvenc_mock.assert_not_called()
            amf_mock.assert_not_called()

    def test_c6_explicit_nvenc_available_uses_nvenc(self) -> None:
        with _Avail(nvenc=True, amf=False):
            self.assertEqual(
                resolve_export_video_encoder(_spec(self.H264), "nvenc"), "h264_nvenc")

    def test_c7_explicit_nvenc_unavailable_falls_back_to_cpu_not_amf(self) -> None:
        # Documented semantics: an explicit hardware choice never silently
        # migrates to the *other* vendor - it falls back to CPU.
        with _Avail(nvenc=False, amf=True):
            self.assertEqual(
                resolve_export_video_encoder(_spec(self.H264), "nvenc"), "libx264")

    def test_c8_explicit_amf_available_uses_amf(self) -> None:
        with _Avail(nvenc=True, amf=True):
            self.assertEqual(
                resolve_export_video_encoder(_spec(self.H265), "amf"), "hevc_amf")

    def test_c9_explicit_amf_unavailable_falls_back_to_cpu(self) -> None:
        with _Avail(nvenc=True, amf=False):
            self.assertEqual(
                resolve_export_video_encoder(_spec(self.H265), "amf"), "libx265")

    def test_c10_unknown_preference_behaves_like_auto(self) -> None:
        for bogus in ("qsv", "", None, "AUTO ", 7):
            with self.subTest(pref=bogus):
                self.assertEqual(ff.normalize_encoder_pref(bogus), "auto")
        with _Avail(nvenc=True, amf=True):
            self.assertEqual(
                resolve_export_video_encoder(_spec(self.H264), "vaapi"), "h264_nvenc")
        with _Avail(nvenc=False, amf=False):
            self.assertEqual(
                resolve_export_video_encoder(_spec(self.H264), "vaapi"), "libx264")

    def test_c10b_known_preferences_normalize_to_themselves(self) -> None:
        for pref in ("auto", "cpu", "nvenc", "amf"):
            self.assertEqual(ff.normalize_encoder_pref(pref), pref)
        self.assertEqual(set(ff.ENCODER_KEY_MAP.values()), {"auto", "cpu", "nvenc", "amf"})
        self.assertEqual(set(ff.ENCODER_KEY_MAP), set(ff.ENCODER_OPTIONS))

    def test_c11_audio_only_format_resolves_to_no_video_encoder(self) -> None:
        with _Avail(nvenc=True, amf=True) as av:
            self.assertIsNone(
                resolve_export_video_encoder(_spec("WAV (audio only)"), "nvenc"))
            for m in av.mocks:
                m.assert_not_called()


# --- Group D: encoder argument groups -------------------------------------


class GroupDEncoderArguments(unittest.TestCase):
    def test_d1_cpu_h264_keeps_the_existing_crf_arguments(self) -> None:
        self.assertEqual(
            build_export_video_encoder_args("libx264", fps=24),
            ["-c:v", "libx264", "-crf", "20", "-preset", "medium", "-r", "24"],
        )
        self.assertEqual(
            build_export_video_encoder_args("libx264"),
            ["-c:v", "libx264", "-crf", "20", "-preset", "medium"],
        )

    def test_d2_nvenc_h264_uses_nvenc_rate_control_and_no_crf(self) -> None:
        args = build_export_video_encoder_args("h264_nvenc", fps=60)
        self.assertEqual(_val(args, "-c:v"), "h264_nvenc")
        self.assertEqual(_val(args, "-rc"), "vbr")
        self.assertEqual(_val(args, "-cq"), "22")
        self.assertEqual(_val(args, "-b:v"), "0")
        self.assertEqual(_val(args, "-r"), "60")
        self.assertNotIn("-crf", args)
        self.assertNotIn("-qp", args)

    def test_d3_amf_h264_uses_cqp_rate_control_and_no_crf(self) -> None:
        args = build_export_video_encoder_args("h264_amf", fps=30)
        self.assertEqual(_val(args, "-c:v"), "h264_amf")
        self.assertEqual(_val(args, "-rc"), "cqp")
        self.assertEqual(_val(args, "-qp_i"), "23")
        self.assertEqual(_val(args, "-qp_p"), "23")
        self.assertEqual(_val(args, "-qp_b"), "23")
        self.assertEqual(_val(args, "-r"), "30")
        self.assertNotIn("-crf", args)
        self.assertNotIn("-cq", args)

    def test_d3b_amf_never_uses_the_generic_qp_ffmpeg_ignores(self) -> None:
        # ffmpeg reports a bare -qp as "has not been used for any stream"
        # for the AMF encoders, which silently drops the quality target.
        for encoder in ("h264_amf", "hevc_amf"):
            with self.subTest(encoder=encoder):
                self.assertNotIn("-qp", build_export_video_encoder_args(encoder))

    def test_d4_hevc_equivalents(self) -> None:
        cpu = build_export_video_encoder_args("libx265")
        self.assertEqual(cpu, ["-c:v", "libx265", "-crf", "24", "-preset", "medium"])

        nv = build_export_video_encoder_args("hevc_nvenc")
        self.assertEqual(_val(nv, "-c:v"), "hevc_nvenc")
        self.assertEqual(_val(nv, "-rc"), "vbr")
        self.assertEqual(_val(nv, "-cq"), "26")
        self.assertEqual(_val(nv, "-b:v"), "0")
        self.assertNotIn("-crf", nv)

        amf = build_export_video_encoder_args("hevc_amf")
        self.assertEqual(_val(amf, "-c:v"), "hevc_amf")
        self.assertEqual(_val(amf, "-rc"), "cqp")
        self.assertEqual(_val(amf, "-qp_i"), "27")
        self.assertEqual(_val(amf, "-qp_p"), "27")
        # hevc_amf has no B-frame QP option.
        self.assertNotIn("-qp_b", amf)
        self.assertNotIn("-crf", amf)

    def test_d5_encoders_without_a_hardware_path_are_untouched(self) -> None:
        self.assertEqual(
            build_export_video_encoder_args("libvpx-vp9"), ["-c:v", "libvpx-vp9"])
        self.assertEqual(
            build_export_video_encoder_args("mpeg4", fps=25),
            ["-c:v", "mpeg4", "-r", "25"])
        self.assertEqual(build_export_video_encoder_args("gif"), ["-c:v", "gif"])

    def test_d5b_vp9_export_stays_on_cpu_and_never_probes(self) -> None:
        with _Avail(nvenc=True, amf=True) as av:
            job = ExportJob(clips=[_clip()], output=Path("o.webm"),
                            fmt_key="WebM (VP9 + Opus)", encoder_pref="nvenc")
            cmd = ExportWorker(job)._build_command()
            self.assertEqual(_val(cmd, "-c:v"), "libvpx-vp9")
            for m in av.mocks:
                m.assert_not_called()

    def test_d6_hardware_codec_names_are_well_formed(self) -> None:
        for name, spec in ff.EXPORT_FORMATS.items():
            with self.subTest(fmt=name):
                if spec.get("nvenc_codec"):
                    self.assertTrue(spec["nvenc_codec"].endswith("_nvenc"))
                    self.assertIsNotNone(spec["vcodec"])
                if spec.get("amf_codec"):
                    self.assertTrue(spec["amf_codec"].endswith("_amf"))
                    self.assertIsNotNone(spec["vcodec"])
                if spec["vcodec"] is None:
                    self.assertIsNone(spec.get("nvenc_codec"))
                    self.assertIsNone(spec.get("amf_codec"))


# --- Group E: filtergraph regression --------------------------------------


class GroupEFiltergraphIsUnchanged(unittest.TestCase):
    def _mixed_clips(self) -> list[Clip]:
        return [
            _clip("a.mp4", start=0.0, width=1920, height=1080),
            _clip("b.mp4", start=2.0, width=640, height=480),
        ]

    def _cmd(self, pref: str, *, nvenc: bool, amf: bool,
             width: int | None = None, height: int | None = None,
             crop: tuple[int, int, int, int] | None = None) -> list[str]:
        job = ExportJob(
            clips=self._mixed_clips(), output=Path("o.mp4"),
            fmt_key="MP4 (H.264 + AAC)", encoder_pref=pref,
            width=width, height=height, crop=crop,
        )
        with _Avail(nvenc=nvenc, amf=amf):
            return ExportWorker(job)._build_command()

    def test_e1_normalization_survives_every_encoder_choice(self) -> None:
        for pref, nv, am, expected in (
            ("cpu", False, False, "libx264"),
            ("auto", True, True, "h264_nvenc"),
            ("nvenc", True, False, "h264_nvenc"),
            ("amf", False, True, "h264_amf"),
        ):
            with self.subTest(pref=pref):
                cmd = self._cmd(pref, nvenc=nv, amf=am)
                self.assertEqual(_val(cmd, "-c:v"), expected)
                graph = _filter_complex(cmd)
                self.assertEqual(graph.count("force_divisible_by=2"), 2)
                self.assertGreaterEqual(graph.count("setsar=1"), 2)
                self.assertGreaterEqual(graph.count("format=yuv420p"), 2)

    def test_e2_the_graph_is_byte_identical_across_encoders(self) -> None:
        cpu = _filter_complex(self._cmd("cpu", nvenc=False, amf=False))
        nv = _filter_complex(self._cmd("nvenc", nvenc=True, amf=False))
        am = _filter_complex(self._cmd("amf", nvenc=False, amf=True))
        auto = _filter_complex(self._cmd("auto", nvenc=True, amf=True))
        self.assertEqual(cpu, nv)
        self.assertEqual(cpu, am)
        self.assertEqual(cpu, auto)

    def test_e3_encoder_flags_live_outside_the_filtergraph(self) -> None:
        cmd = self._cmd("nvenc", nvenc=True, amf=False)
        graph = _filter_complex(cmd)
        for token in ("h264_nvenc", "-rc", "vbr", "-cq"):
            self.assertNotIn(token, graph)
        self.assertIn("h264_nvenc", cmd)

    def test_e4_pixel_format_normalization_is_kept_in_the_output_args(self) -> None:
        cmd = self._cmd("nvenc", nvenc=True, amf=False)
        self.assertEqual(_val(cmd, "-pix_fmt"), "yuv420p")

    def test_e5_crop_precedence_is_unchanged_by_the_encoder(self) -> None:
        crop = (10, 20, 640, 360)
        cpu = self._cmd("cpu", nvenc=False, amf=False, crop=crop, width=1920, height=1080)
        nv = self._cmd("nvenc", nvenc=True, amf=False, crop=crop, width=1920, height=1080)
        self.assertEqual(_filter_complex(cpu), _filter_complex(nv))
        self.assertIn("crop=640:360:10:20", _filter_complex(nv))
        # crop still wins over the explicit resolution preset
        self.assertIn("scale=640:360", _filter_complex(nv))


# --- Group F: resolution regression ---------------------------------------


class GroupFResolutionIsUnchanged(unittest.TestCase):
    def _cmd(self, pref: str, *, nvenc: bool, amf: bool,
             width: int | None, height: int | None) -> list[str]:
        job = ExportJob(
            clips=[_clip("a.mp4", width=1920, height=1080)],
            output=Path("o.mp4"), fmt_key="MP4 (H.264 + AAC)",
            encoder_pref=pref, width=width, height=height,
        )
        with _Avail(nvenc=nvenc, amf=amf):
            return ExportWorker(job)._build_command()

    def test_f1_explicit_720p_target_is_identical_on_cpu_and_hardware(self) -> None:
        cpu = self._cmd("cpu", nvenc=False, amf=False, width=1280, height=720)
        nv = self._cmd("nvenc", nvenc=True, amf=False, width=1280, height=720)
        am = self._cmd("amf", nvenc=False, amf=True, width=1280, height=720)
        self.assertEqual(_filter_complex(cpu), _filter_complex(nv))
        self.assertEqual(_filter_complex(cpu), _filter_complex(am))
        self.assertIn("scale=1280:720", _filter_complex(nv))
        self.assertIn("pad=1280:720", _filter_complex(nv))

    def test_f2_auto_target_is_identical_on_cpu_and_hardware(self) -> None:
        cpu = self._cmd("cpu", nvenc=False, amf=False, width=None, height=None)
        nv = self._cmd("auto", nvenc=True, amf=False, width=None, height=None)
        self.assertEqual(_filter_complex(cpu), _filter_complex(nv))
        self.assertIn("scale=1920:1080", _filter_complex(nv))

    def test_f3_only_the_encoder_argument_group_differs(self) -> None:
        cpu = self._cmd("cpu", nvenc=False, amf=False, width=1280, height=720)
        nv = self._cmd("nvenc", nvenc=True, amf=False, width=1280, height=720)
        cpu_head = cpu[:cpu.index("-c:v")]
        nv_head = nv[:nv.index("-c:v")]
        self.assertEqual(cpu_head, nv_head)
        cpu_tail = cpu[cpu.index("-c:a"):]
        nv_tail = nv[nv.index("-c:a"):]
        self.assertEqual(cpu_tail, nv_tail)


# --- Group G: default ExportJob compatibility -----------------------------


class GroupGDefaultExportJob(unittest.TestCase):
    def test_g1_export_job_constructs_without_an_encoder_preference(self) -> None:
        job = ExportJob(clips=[_clip()], output=Path("o.mp4"),
                        fmt_key="MP4 (H.264 + AAC)")
        self.assertEqual(job.encoder_pref, "auto")

    def test_g2_the_default_job_exports_on_cpu_with_no_gpu(self) -> None:
        job = ExportJob(clips=[_clip()], output=Path("o.mp4"),
                        fmt_key="MP4 (H.264 + AAC)")
        with _Avail(nvenc=False, amf=False):
            cmd = ExportWorker(job)._build_command()
        self.assertEqual(_val(cmd, "-c:v"), "libx264")
        self.assertEqual(_val(cmd, "-crf"), "20")
        self.assertEqual(_val(cmd, "-preset"), "medium")

    def test_g3_positional_construction_still_works(self) -> None:
        job = ExportJob([_clip()], Path("o.mp4"), "MP4 (H.264 + AAC)")
        self.assertEqual(job.encoder_pref, "auto")


# --- Group H: UI + QSettings ----------------------------------------------


class _FakeQSettings:
    """Dict-backed stand-in with QSettings' value()/setValue() shape."""

    store: dict[str, object] = {}

    def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        self.args = args

    def value(self, key, default=None):  # noqa: ANN001
        return type(self).store.get(key, default)

    def setValue(self, key, value) -> None:  # noqa: ANN001
        type(self).store[key] = value


def _settings(initial: dict | None = None):
    _FakeQSettings.store = dict(initial or {})
    return patch.object(app_mod, "QSettings", _FakeQSettings)


_ALL_HW_WORKS = {e: True for e in ("h264_nvenc", "hevc_nvenc",
                                   "h264_amf", "hevc_amf")}


class _BareWindow:
    """Minimal `MainWindow.__new__` host for the encoder-combo methods."""

    def __init__(self, fmt: str = "MP4 (H.264 + AAC)") -> None:
        self.win = app_mod.MainWindow.__new__(app_mod.MainWindow)
        self.win._nvenc_available = False
        self.win._amf_available = False
        self.win._encoder_caps = {}
        fmt_combo = QComboBox()
        for key in ff.EXPORT_FORMATS:
            fmt_combo.addItem(key)
        fmt_combo.setCurrentText(fmt)
        self.win.format_combo = fmt_combo


class GroupHEncoderUiAndSettings(unittest.TestCase):
    def _combo(self) -> QComboBox:
        host = _BareWindow()
        combo = app_mod.MainWindow._build_encoder_combo(host.win)
        self._host = host
        return combo

    def test_h1_the_encoder_combo_offers_every_preference(self) -> None:
        with _settings():
            combo = self._combo()
        self.assertEqual(combo.count(), len(ff.ENCODER_OPTIONS))
        keys = [combo.itemData(i) for i in range(combo.count())]
        self.assertEqual(keys, ["auto", "cpu", "nvenc", "amf"])
        self.assertEqual(
            [combo.itemText(i) for i in range(combo.count())], ff.ENCODER_OPTIONS)

    def test_h2_the_resolution_combo_is_still_built_by_the_export_bar(self) -> None:
        import inspect
        src = inspect.getsource(app_mod.MainWindow._build_ui)
        self.assertIn("self.resolution_combo", src)
        self.assertIn("Auto (First Clip)", src)
        self.assertIn("1280 × 720", src)
        self.assertIn("self.encoder_combo", src)

    def test_h3_the_default_preference_is_automatic(self) -> None:
        with _settings():
            combo = self._combo()
            self.assertEqual(combo.currentIndex(), 0)
            self.assertEqual(combo.currentData(), "auto")
            self.assertEqual(app_mod.load_encoder_pref(), "auto")

    def test_h4_a_stored_cpu_preference_restores(self) -> None:
        with _settings({app_mod.ENCODER_SETTINGS_KEY: "cpu"}):
            self.assertEqual(app_mod.load_encoder_pref(), "cpu")
            combo = self._combo()
            self.assertEqual(combo.currentData(), "cpu")

    def test_h5_a_stored_hardware_preference_restores(self) -> None:
        for pref in ("nvenc", "amf"):
            with self.subTest(pref=pref):
                with _settings({app_mod.ENCODER_SETTINGS_KEY: pref}):
                    combo = self._combo()
                    self.assertEqual(combo.currentData(), pref)

    def test_h6_an_unknown_stored_value_normalizes_to_automatic(self) -> None:
        for bogus in ("qsv", "", None, 42, "NVIDIA GPU (NVENC)"):
            with self.subTest(stored=bogus):
                with _settings({app_mod.ENCODER_SETTINGS_KEY: bogus}):
                    self.assertEqual(app_mod.load_encoder_pref(), "auto")
                    combo = self._combo()
                    self.assertEqual(combo.currentData(), "auto")

    def test_h6b_selecting_a_preference_persists_the_internal_key(self) -> None:
        with _settings():
            combo = self._combo()
            self._host.win.encoder_combo = combo
            combo.setCurrentIndex(2)
            app_mod.MainWindow._on_encoder_changed(self._host.win, 2)
            self.assertEqual(_FakeQSettings.store[app_mod.ENCODER_SETTINGS_KEY], "nvenc")

    def test_h6c_no_hardware_capability_is_ever_persisted(self) -> None:
        with _settings():
            combo = self._combo()
            self._host.win.encoder_combo = combo
            app_mod.MainWindow._apply_encoder_caps(self._host.win, _ALL_HW_WORKS)
            self.assertEqual(list(_FakeQSettings.store), [])

    def test_h7_audio_only_disables_the_encoder_and_resolution_controls(self) -> None:
        self.assertFalse(app_mod.visual_export_controls_enabled(
            has_clips=True, has_added_audio=False, audio_only=True, exporting=False))
        self.assertTrue(app_mod.visual_export_controls_enabled(
            has_clips=True, has_added_audio=False, audio_only=False, exporting=False))
        self.assertFalse(app_mod.visual_export_controls_enabled(
            has_clips=False, has_added_audio=True, audio_only=False, exporting=False))
        self.assertFalse(app_mod.visual_export_controls_enabled(
            has_clips=True, has_added_audio=False, audio_only=False, exporting=True))

    def test_h8_probe_results_update_items_without_rebuilding_the_combo(self) -> None:
        with _settings():
            combo = self._combo()
        win = self._host.win
        win.encoder_combo = combo
        sentinel = QComboBox()
        sentinel.addItem("Auto (First Clip)", None)
        win.resolution_combo = sentinel
        before_texts = [combo.itemText(i) for i in range(combo.count())]

        app_mod.MainWindow._apply_encoder_caps(win, {e: False for e in _ALL_HW_WORKS})
        self.assertIs(win.encoder_combo, combo)
        self.assertIs(win.resolution_combo, sentinel)
        self.assertEqual(sentinel.count(), 1)
        self.assertEqual(combo.count(), len(before_texts))
        model = combo.model()
        self.assertFalse(model.item(2).isEnabled())
        self.assertFalse(model.item(3).isEnabled())
        self.assertTrue(model.item(0).isEnabled())
        self.assertTrue(model.item(1).isEnabled())

    def test_h8b_available_hardware_is_re_enabled_and_relabelled(self) -> None:
        with _settings():
            combo = self._combo()
        win = self._host.win
        win.encoder_combo = combo
        app_mod.MainWindow._apply_encoder_caps(win, {e: False for e in _ALL_HW_WORKS})
        self.assertIn("unavailable", combo.itemText(2))
        app_mod.MainWindow._apply_encoder_caps(win, _ALL_HW_WORKS)
        self.assertEqual(combo.itemText(2), ff.ENCODER_OPTIONS[2])
        self.assertTrue(combo.model().item(2).isEnabled())

    def test_h9_an_unavailable_selection_reverts_to_automatic(self) -> None:
        with _settings({app_mod.ENCODER_SETTINGS_KEY: "nvenc"}):
            combo = self._combo()
            win = self._host.win
            win.encoder_combo = combo
            self.assertEqual(combo.currentData(), "nvenc")
            app_mod.MainWindow._apply_encoder_caps(
                win, {e: False for e in _ALL_HW_WORKS})
            self.assertEqual(combo.currentData(), "auto")
            self.assertEqual(
                app_mod.MainWindow._selected_encoder_pref(win), "auto")
            # The stored preference is untouched, so it returns on a
            # machine that can honour it.
            self.assertEqual(
                _FakeQSettings.store[app_mod.ENCODER_SETTINGS_KEY], "nvenc")

    def test_h10_availability_is_per_format_not_per_vendor(self) -> None:
        """h264_nvenc working must not enable NVIDIA for an H.265 export."""
        caps = dict(_ALL_HW_WORKS, hevc_nvenc=False, hevc_amf=False)
        with _settings():
            host = _BareWindow(fmt="MP4 (H.264 + AAC)")
            self._host = host
            combo = app_mod.MainWindow._build_encoder_combo(host.win)
        host.win.encoder_combo = combo
        app_mod.MainWindow._apply_encoder_caps(host.win, caps)
        self.assertTrue(combo.model().item(2).isEnabled())
        self.assertTrue(combo.model().item(3).isEnabled())

        host.win.format_combo.setCurrentText("MP4 (H.265 + AAC)")
        app_mod.MainWindow._on_format_changed(host.win, "MP4 (H.265 + AAC)")
        self.assertFalse(combo.model().item(2).isEnabled())
        self.assertIn("unavailable", combo.itemText(2))
        self.assertFalse(combo.model().item(3).isEnabled())

        # ...and it comes back when the format does.
        host.win.format_combo.setCurrentText("MKV (H.264 + AAC)")
        app_mod.MainWindow._on_format_changed(host.win, "MKV (H.264 + AAC)")
        self.assertTrue(combo.model().item(2).isEnabled())
        self.assertEqual(combo.itemText(2), ff.ENCODER_OPTIONS[2])

    def test_h11_formats_with_no_hardware_path_disable_both_vendors(self) -> None:
        with _settings():
            host = _BareWindow(fmt="WebM (VP9 + Opus)")
            self._host = host
            combo = app_mod.MainWindow._build_encoder_combo(host.win)
        host.win.encoder_combo = combo
        app_mod.MainWindow._apply_encoder_caps(host.win, _ALL_HW_WORKS)
        self.assertFalse(combo.model().item(2).isEnabled())
        self.assertFalse(combo.model().item(3).isEnabled())
        self.assertFalse(host.win._nvenc_available)
        self.assertFalse(host.win._amf_available)

    # --- pending capability is not "unavailable" --------------------------
    #
    # `_encoder_caps` is empty until the background probe reports. Anything
    # that refreshes the combo before then - a format change, an export-type
    # change - must not read "not tested yet" as "tested and unusable", or a
    # persisted hardware preference is silently rewritten to Automatic during
    # a slow startup probe and never restored when the probe succeeds.

    def _pending_host(self, pref: str, fmt: str = "MP4 (H.264 + AAC)"):
        """A window whose probe has not reported anything yet."""
        with _settings({app_mod.ENCODER_SETTINGS_KEY: pref}):
            host = _BareWindow(fmt=fmt)
            self._host = host
            combo = app_mod.MainWindow._build_encoder_combo(host.win)
        host.win.encoder_combo = combo
        self.assertEqual(host.win._encoder_caps, {})
        return host, combo

    def _item(self, combo: QComboBox, key: str):
        idx = combo.findData(key)
        self.assertGreaterEqual(idx, 0)
        return idx, combo.model().item(idx)

    def test_h13_a_pending_probe_keeps_a_persisted_nvenc_preference(self) -> None:
        host, combo = self._pending_host("nvenc")
        with _settings({app_mod.ENCODER_SETTINGS_KEY: "nvenc"}):
            app_mod.MainWindow._on_format_changed(host.win, "MP4 (H.264 + AAC)")
        idx, item = self._item(combo, "nvenc")
        self.assertEqual(combo.currentData(), "nvenc")
        self.assertTrue(item.isEnabled())
        self.assertNotIn("unavailable", combo.itemText(idx))
        self.assertEqual(
            app_mod.MainWindow._selected_encoder_pref(host.win), "nvenc")

    def test_h13b_a_pending_probe_keeps_a_persisted_amf_preference(self) -> None:
        host, combo = self._pending_host("amf")
        with _settings({app_mod.ENCODER_SETTINGS_KEY: "amf"}):
            app_mod.MainWindow._on_format_changed(host.win, "MP4 (H.264 + AAC)")
        idx, item = self._item(combo, "amf")
        self.assertEqual(combo.currentData(), "amf")
        self.assertTrue(item.isEnabled())
        self.assertNotIn("unavailable", combo.itemText(idx))

    def test_h14_a_probed_unavailable_encoder_still_reverts_to_automatic(
        self,
    ) -> None:
        """Once the answer really is known, the existing safe behavior is
        unchanged - including leaving the stored preference alone."""
        host, combo = self._pending_host("nvenc")
        app_mod.MainWindow._apply_encoder_caps(
            host.win, {e: False for e in _ALL_HW_WORKS})
        idx, item = self._item(combo, "nvenc")
        self.assertEqual(combo.currentData(), "auto")
        self.assertFalse(item.isEnabled())
        self.assertIn("unavailable", combo.itemText(idx))
        self.assertEqual(
            _FakeQSettings.store[app_mod.ENCODER_SETTINGS_KEY], "nvenc")

    def test_h15_a_probed_available_encoder_stays_selected(self) -> None:
        host, combo = self._pending_host("nvenc")
        app_mod.MainWindow._apply_encoder_caps(host.win, _ALL_HW_WORKS)
        idx, item = self._item(combo, "nvenc")
        self.assertEqual(combo.currentData(), "nvenc")
        self.assertTrue(item.isEnabled())
        self.assertEqual(combo.itemText(idx), ff.ENCODER_OPTIONS[idx])
        self.assertTrue(host.win._nvenc_available)

    def test_h16_one_backend_s_result_does_not_classify_the_other(self) -> None:
        """A partial report - NVENC answered, AMF still outstanding - must
        leave AMF pending rather than declaring it unusable."""
        host, combo = self._pending_host("amf")
        app_mod.MainWindow._apply_encoder_caps(
            host.win, {"h264_nvenc": True, "hevc_nvenc": True})
        nv_idx, nv_item = self._item(combo, "nvenc")
        amf_idx, amf_item = self._item(combo, "amf")
        self.assertTrue(nv_item.isEnabled())
        self.assertTrue(host.win._nvenc_available)
        # AMF was never reported, so it is still pending, not unavailable.
        self.assertTrue(amf_item.isEnabled())
        self.assertNotIn("unavailable", combo.itemText(amf_idx))
        self.assertEqual(combo.currentData(), "amf")
        # ...and it is not claimed as detected hardware either.
        self.assertFalse(host.win._amf_available)

    def test_h17_cpu_and_automatic_are_untouched_while_pending(self) -> None:
        for pref in ("cpu", "auto"):
            with self.subTest(pref=pref):
                host, combo = self._pending_host(pref)
                with _settings({app_mod.ENCODER_SETTINGS_KEY: pref}):
                    app_mod.MainWindow._on_format_changed(
                        host.win, "MP4 (H.264 + AAC)")
                for key in ("auto", "cpu"):
                    idx, item = self._item(combo, key)
                    self.assertTrue(item.isEnabled())
                    self.assertEqual(combo.itemText(idx), ff.ENCODER_OPTIONS[idx])
                self.assertEqual(combo.currentData(), pref)
                # And still untouched once real results land.
                app_mod.MainWindow._apply_encoder_caps(
                    host.win, {e: False for e in _ALL_HW_WORKS})
                for key in ("auto", "cpu"):
                    idx, item = self._item(combo, key)
                    self.assertTrue(item.isEnabled())
                    self.assertEqual(combo.itemText(idx), ff.ENCODER_OPTIONS[idx])
                self.assertEqual(combo.currentData(), pref)

    def test_h18_a_pending_probe_is_not_reported_as_no_hardware(self) -> None:
        """The tooltip is the other place "unknown" used to read as a
        settled negative."""
        host, combo = self._pending_host("auto")
        with _settings({app_mod.ENCODER_SETTINGS_KEY: "auto"}):
            app_mod.MainWindow._on_format_changed(host.win, "MP4 (H.264 + AAC)")
        self.assertNotIn("No usable", combo.toolTip())

    # --- the tooltip must not present a partial probe as a finished one ---

    _CHECKING = "Checking for GPU support"

    def test_h18b_a_detected_backend_does_not_hide_a_still_pending_one(
        self,
    ) -> None:
        """NVENC answered, AMF outstanding: reporting only the detected
        hardware presents an unfinished probe as a settled result."""
        host, combo = self._pending_host("auto")
        app_mod.MainWindow._apply_encoder_caps(
            host.win, {"h264_nvenc": True, "hevc_nvenc": True})
        tip = combo.toolTip()
        self.assertIn("NVIDIA (NVENC)", tip)
        self.assertIn(self._CHECKING, tip)
        self.assertNotIn("AMD (AMF)", tip)

    def test_h18c_the_same_holds_with_the_backends_reversed(self) -> None:
        host, combo = self._pending_host("auto")
        app_mod.MainWindow._apply_encoder_caps(
            host.win, {"h264_amf": True, "hevc_amf": True})
        tip = combo.toolTip()
        self.assertIn("AMD (AMF)", tip)
        self.assertIn(self._CHECKING, tip)
        self.assertNotIn("NVIDIA (NVENC)", tip)

    def test_h18d_the_pending_wording_goes_away_once_everything_settles(
        self,
    ) -> None:
        host, combo = self._pending_host("auto")
        app_mod.MainWindow._apply_encoder_caps(
            host.win, {"h264_nvenc": True, "hevc_nvenc": True})
        self.assertIn(self._CHECKING, combo.toolTip())
        app_mod.MainWindow._apply_encoder_caps(
            host.win, dict(_ALL_HW_WORKS, h264_amf=False, hevc_amf=False))
        tip = combo.toolTip()
        self.assertIn("NVIDIA (NVENC)", tip)
        self.assertNotIn(self._CHECKING, tip)
        self.assertNotIn("No usable", tip)

    def test_h18e_all_settled_and_unusable_keeps_the_unavailable_tooltip(
        self,
    ) -> None:
        host, combo = self._pending_host("auto")
        app_mod.MainWindow._apply_encoder_caps(
            host.win, {e: False for e in _ALL_HW_WORKS})
        tip = combo.toolTip()
        self.assertIn("No usable", tip)
        self.assertNotIn(self._CHECKING, tip)

    def test_h18f_all_settled_and_usable_keeps_the_detected_tooltip(
        self,
    ) -> None:
        host, combo = self._pending_host("auto")
        app_mod.MainWindow._apply_encoder_caps(host.win, _ALL_HW_WORKS)
        tip = combo.toolTip()
        self.assertIn("NVIDIA (NVENC)", tip)
        self.assertIn("AMD (AMF)", tip)
        self.assertNotIn(self._CHECKING, tip)
        self.assertNotIn("No usable", tip)

    def test_h19_a_stale_preference_still_normalizes(self) -> None:
        host, combo = self._pending_host("wildly-unknown")
        self.assertEqual(combo.currentData(), "auto")
        self.assertEqual(
            app_mod.MainWindow._selected_encoder_pref(host.win), "auto")

    def test_h12_audio_only_formats_report_no_hardware(self) -> None:
        with _settings():
            host = _BareWindow(fmt="WAV (audio only)")
            self._host = host
            combo = app_mod.MainWindow._build_encoder_combo(host.win)
        host.win.encoder_combo = combo
        app_mod.MainWindow._apply_encoder_caps(host.win, _ALL_HW_WORKS)
        for key in ("nvenc", "amf"):
            self.assertFalse(
                app_mod.MainWindow._hardware_available_for_current_format(
                    host.win, key))


# --- Group I: background probe worker -------------------------------------


class GroupIProbeWorker(unittest.TestCase):
    def test_i1_the_worker_emits_one_result_per_hardware_encoder(self) -> None:
        worker = app_mod.HardwareProbeWorker()
        seen: list[dict] = []
        done: list[int] = []
        worker.probed.connect(seen.append)
        worker.finished.connect(lambda: done.append(1))
        with patch.object(ff, "hardware_encoder_available",
                          lambda e: e.endswith("_nvenc")):
            worker.run()
        self.assertEqual(seen, [{
            "h264_nvenc": True, "hevc_nvenc": True,
            "h264_amf": False, "hevc_amf": False,
        }])
        self.assertEqual(done, [1])

    def test_i2_a_failing_probe_still_finishes_with_a_safe_result(self) -> None:
        worker = app_mod.HardwareProbeWorker()
        seen: list[dict] = []
        done: list[int] = []
        worker.probed.connect(seen.append)
        worker.finished.connect(lambda: done.append(1))
        with patch.object(ff, "hardware_encoder_available",
                          side_effect=RuntimeError("boom")):
            worker.run()
        self.assertEqual(seen, [{e: False for e in ff.HARDWARE_ENCODERS}])
        self.assertEqual(done, [1])

    def test_i2b_the_worker_touches_no_widgets(self) -> None:
        import inspect
        src = inspect.getsource(app_mod.HardwareProbeWorker)
        for banned in ("combo", "setEnabled", "setItemText", "setCurrentIndex"):
            self.assertNotIn(banned, src)

    def test_i3_completion_clears_the_thread_and_worker_handles(self) -> None:
        win = app_mod.MainWindow.__new__(app_mod.MainWindow)
        win._encoder_probe_thread = object()
        win._encoder_probe_worker = object()
        app_mod.MainWindow._on_encoder_probe_done(win)
        self.assertIsNone(win._encoder_probe_thread)
        self.assertIsNone(win._encoder_probe_worker)

    def test_i4_shutdown_cancels_and_joins_a_running_probe_thread(self) -> None:
        win = app_mod.MainWindow.__new__(app_mod.MainWindow)
        thread = _FakeThread(True, wait_result=True)
        worker = app_mod.HardwareProbeWorker()
        win._encoder_probe_thread = thread
        win._encoder_probe_worker = worker
        before = len(app_mod._ORPHANED_PROBE_THREADS)
        app_mod.MainWindow._stop_encoder_probe(win)
        self.assertTrue(worker._stop_requested())
        self.assertEqual(thread.quit_calls, 1)
        self.assertEqual(thread.wait_args, [app_mod.ENCODER_PROBE_SHUTDOWN_GRACE_MS])
        # It stopped in time, so nothing needs parking.
        self.assertEqual(len(app_mod._ORPHANED_PROBE_THREADS), before)
        self.assertIsNone(win._encoder_probe_thread)

    def test_i4c_a_probe_that_outlives_the_grace_period_is_parked(self) -> None:
        win = app_mod.MainWindow.__new__(app_mod.MainWindow)
        thread = _FakeThread(True, wait_result=False)
        worker = app_mod.HardwareProbeWorker()
        win._encoder_probe_thread = thread
        win._encoder_probe_worker = worker
        before = len(app_mod._ORPHANED_PROBE_THREADS)
        try:
            app_mod.MainWindow._stop_encoder_probe(win)
            # Destroying a running QThread aborts the process, so the
            # reference has to survive the window.
            self.assertEqual(len(app_mod._ORPHANED_PROBE_THREADS), before + 1)
            self.assertIs(app_mod._ORPHANED_PROBE_THREADS[-1][0], thread)
        finally:
            del app_mod._ORPHANED_PROBE_THREADS[before:]

    def test_i8_cancel_ends_the_running_probe_child(self) -> None:
        worker = app_mod.HardwareProbeWorker()
        with patch.object(ff, "abort_hardware_probes") as abort:
            worker.cancel()
        abort.assert_called_once_with()
        self.assertTrue(worker._stop_requested())

    def test_i6_a_cancelled_worker_reports_nothing_but_still_finishes(self) -> None:
        worker = app_mod.HardwareProbeWorker()
        seen: list[dict] = []
        done: list[int] = []
        worker.probed.connect(seen.append)
        worker.finished.connect(lambda: done.append(1))
        with patch.object(ff, "abort_hardware_probes"):
            worker.cancel()
        with patch.object(ff, "hardware_encoder_available", return_value=True):
            worker.run()
        self.assertEqual(seen, [])
        self.assertEqual(done, [1])

    def test_i7_cancelling_mid_run_skips_the_probes_not_started_yet(self) -> None:
        worker = app_mod.HardwareProbeWorker()
        probed: list[str] = []

        def _fake_available(encoder: str) -> bool:
            probed.append(encoder)
            with patch.object(ff, "abort_hardware_probes"):
                worker.cancel()   # shutdown arrives during the first probe
            return False

        with patch.object(ff, "hardware_encoder_available", _fake_available):
            worker.run()
        self.assertEqual(probed, [ff.HARDWARE_ENCODERS[0]])

    def test_i7b_an_uncancelled_run_probes_every_encoder(self) -> None:
        worker = app_mod.HardwareProbeWorker()
        probed: list[str] = []
        with patch.object(ff, "hardware_encoder_available",
                          lambda e: probed.append(e) or False):
            worker.run()
        self.assertEqual(probed, list(ff.HARDWARE_ENCODERS))

    def test_i4b_shutdown_is_a_no_op_when_no_probe_ran(self) -> None:
        win = app_mod.MainWindow.__new__(app_mod.MainWindow)
        win._encoder_probe_thread = None
        win._encoder_probe_worker = None
        app_mod.MainWindow._stop_encoder_probe(win)  # must not raise
        idle = _FakeThread(False)
        win._encoder_probe_thread = idle
        app_mod.MainWindow._stop_encoder_probe(win)
        self.assertEqual(idle.quit_calls, 0)

    def test_i5_close_event_stops_the_probe(self) -> None:
        import inspect
        src = inspect.getsource(app_mod.MainWindow.closeEvent)
        self.assertIn("_stop_encoder_probe", src)


class _FakeThread:
    """QThread stand-in with the exact surface `_stop_encoder_probe` uses.

    `wait()` returns a bool like the real one, so the "did not stop in
    time" branch is reachable.
    """

    def __init__(self, running: bool, *, wait_result: bool = True) -> None:
        self._running = running
        self._wait_result = wait_result
        self.quit_calls = 0
        self.wait_args: list[int] = []

    def isRunning(self) -> bool:
        return self._running

    def quit(self) -> None:
        self.quit_calls += 1

    def wait(self, ms: int) -> bool:
        self.wait_args.append(ms)
        return self._wait_result


# --- Group J: audio only ---------------------------------------------------


class GroupJAudioOnly(unittest.TestCase):
    def _cmd(self, pref: str) -> list[str]:
        job = ExportJob(
            clips=[_clip()], output=Path("o.wav"), fmt_key="WAV (audio only)",
            encoder_pref=pref,
            audio_tracks=[AudioTrack(path=Path("m.mp3"), volume=0.5, duration=2.0)],
        )
        with _Avail(nvenc=True, amf=True) as av:
            cmd = ExportWorker(job)._build_command()
            self._mocks = av.mocks
        return cmd

    def test_j1_audio_only_export_carries_no_video_encoder_arguments(self) -> None:
        cmd = self._cmd("nvenc")
        for token in ("-c:v", "h264_nvenc", "hevc_nvenc", "h264_amf", "hevc_amf",
                      "-cq", "-qp", "-crf", "-rc", "-pix_fmt"):
            self.assertNotIn(token, cmd)
        self.assertEqual(_val(cmd, "-c:a"), "pcm_s16le")

    def test_j2_audio_only_export_never_probes_hardware(self) -> None:
        self._cmd("auto")
        for m in self._mocks:
            m.assert_not_called()

    def test_j3_audio_only_command_is_identical_across_preferences(self) -> None:
        self.assertEqual(self._cmd("cpu"), self._cmd("nvenc"))
        self.assertEqual(self._cmd("cpu"), self._cmd("amf"))
        self.assertEqual(self._cmd("cpu"), self._cmd("auto"))


# --- Group K: lifecycle transaction ---------------------------------------
#
# Lifecycle state decides whether a cache write is legal, so the two must be
# owned by one lock. These tests pin the transaction itself: eligibility and
# commit are indivisible, an aborted generation can publish nothing, and a
# result that was already published stays published.


def _abort_state_now() -> None:
    """Apply what a shutdown does to lifecycle state, from a caller that is
    *already inside* the lifecycle critical section.

    `abort_hardware_probes()` takes the lifecycle lock itself, so a hook
    firing from within a critical section has to move the watermark
    directly. Same state change, no re-entrancy.
    """
    ff._aborted_through = max(ff._aborted_through, ff._probe_generation)


class _WatchedLock:
    """The real lifecycle lock, with observable boundaries.

    Counts how many critical sections a call takes and can run a hook at a
    chosen boundary, which is how a shutdown is placed at an exact point in
    a commit without any sleep. `on_enter` runs with the lock held and
    `on_exit` runs just before it is dropped, so both see the same state
    production does.
    """

    def __init__(self, real, *, on_enter=None, on_exit=None) -> None:
        self._real = real
        self._on_enter = on_enter
        self._on_exit = on_exit
        self.holds = 0

    def acquire(self, *args, **kwargs):  # noqa: ANN001
        got = self._real.acquire(*args, **kwargs)
        if got:
            self.holds += 1
            if self._on_enter is not None:
                self._on_enter()
        return got

    def release(self) -> None:
        if self._on_exit is not None:
            self._on_exit()
        self._real.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc) -> bool:
        self.release()
        return False


class GroupKLifecycleTransaction(_HardwareProbeCase):
    # --- T1: availability commit ------------------------------------------

    def test_t1a_availability_commit_is_one_critical_section(self) -> None:
        """Eligibility and the write cannot be split: taking the lifecycle
        lock twice is exactly the window an abort used to slip through."""
        gen = ff.current_probe_generation()
        watched = _WatchedLock(ff._lifecycle_lock)
        with patch.object(ff, "_lifecycle_lock", watched):
            ff._commit_encoder_result(gen, "h264_nvenc", True)
        self.assertEqual(watched.holds, 1)
        self.assertIs(ff._encoder_cache["h264_nvenc"], True)

    def test_t1b_an_abort_reaching_the_commit_first_discards_the_result(
        self,
    ) -> None:
        """The shutdown lands the instant the commit takes the lock. Because
        the check happens inside that same section, it sees the abort."""
        gen = ff.current_probe_generation()
        watched = _WatchedLock(ff._lifecycle_lock, on_enter=_abort_state_now)
        with patch.object(ff, "_lifecycle_lock", watched):
            ff._commit_encoder_result(gen, "h264_nvenc", True)
        self.assertTrue(ff.hardware_probes_aborted(gen))
        self.assertNotIn("h264_nvenc", ff._encoder_cache)

    def test_t1c_availability_publishes_through_the_atomic_commit(self) -> None:
        """End to end: the probe's own captured generation - the default
        generation 0 here - is what the commit is judged against."""
        seen: list[tuple] = []
        real = ff._commit_encoder_result

        def _spy(generation, encoder, result):  # noqa: ANN001
            seen.append((generation, encoder, result))
            real(generation, encoder, result)

        fake = _FakeFFmpeg(init_returncode=0)
        with _Patched(fake), patch.object(ff, "_commit_encoder_result", _spy):
            self.assertTrue(ff.nvenc_available("h264_nvenc"))
        self.assertEqual(seen, [(0, "h264_nvenc", True)])
        self.assertIs(ff._encoder_cache["h264_nvenc"], True)

    # --- T2: listing commit -----------------------------------------------

    def test_t2a_listing_commit_is_one_critical_section(self) -> None:
        gen = ff.current_probe_generation()
        watched = _WatchedLock(ff._lifecycle_lock)
        with patch.object(ff, "_lifecycle_lock", watched):
            ff._commit_listing(gen, _LISTING)
        self.assertEqual(watched.holds, 1)
        self.assertEqual(ff._encoder_listing, _LISTING)

    def test_t2b_an_abort_reaching_the_listing_commit_first_discards_it(
        self,
    ) -> None:
        gen = ff.current_probe_generation()
        watched = _WatchedLock(ff._lifecycle_lock, on_enter=_abort_state_now)
        with patch.object(ff, "_lifecycle_lock", watched):
            ff._commit_listing(gen, _LISTING)
        self.assertTrue(ff.hardware_probes_aborted(gen))
        self.assertIsNone(ff._encoder_listing)

    def test_t2c_the_first_valid_listing_writer_wins(self) -> None:
        """A redundant later probe must not clobber the listing this
        process already settled on."""
        gen = ff.current_probe_generation()
        ff._commit_listing(gen, _LISTING)
        ff._commit_listing(gen, "late and different\n")
        self.assertEqual(ff._encoder_listing, _LISTING)

    def test_t2d_listing_publishes_through_the_atomic_commit(self) -> None:
        seen: list[tuple] = []
        real = ff._commit_listing

        def _spy(generation, listing):  # noqa: ANN001
            seen.append((generation, listing))
            real(generation, listing)

        fake = _FakeFFmpeg()
        with _Patched(fake), patch.object(ff, "_commit_listing", _spy):
            listing = ff._ffmpeg_encoder_listing("/fake/bin/ffmpeg", 0)
        self.assertEqual(listing, _LISTING)
        self.assertEqual(seen, [(0, _LISTING)])

    # --- T3/T4: a late generation cannot reach the cache -------------------

    def test_t3_a_late_negative_from_an_aborted_generation_is_discarded(
        self,
    ) -> None:
        old = ff.current_probe_generation()
        ff.abort_hardware_probes()
        ff.clear_hardware_probe_abort()
        new = ff.current_probe_generation()
        self.assertNotEqual(new, old)

        ff._commit_encoder_result(old, "h264_nvenc", False)
        self.assertNotIn("h264_nvenc", ff._encoder_cache)

        # The live generation probes for itself and caches normally.
        fake = _FakeFFmpeg(init_returncode=0)
        with _Patched(fake):
            self.assertTrue(ff.nvenc_available("h264_nvenc"))
        self.assertEqual(len(fake.init_calls), 1)
        self.assertIs(ff._encoder_cache["h264_nvenc"], True)

    def test_t4_a_late_positive_from_an_aborted_generation_is_discarded(
        self,
    ) -> None:
        old = ff.current_probe_generation()
        ff.abort_hardware_probes()
        ff.clear_hardware_probe_abort()

        ff._commit_encoder_result(old, "h264_amf", True)
        self.assertNotIn("h264_amf", ff._encoder_cache)

        # AMF really does not initialize here, and that is the answer that
        # sticks - the stale True never enabled it.
        fake = _FakeFFmpeg(init_returncode=1)
        with _Patched(fake):
            self.assertFalse(ff.amf_available("h264_amf"))
        self.assertIs(ff._encoder_cache["h264_amf"], False)

    # --- T5: a new generation runs while an old one is parked -------------

    def test_t5_a_new_generation_commits_while_an_old_probe_is_parked(
        self,
    ) -> None:
        entered = threading.Event()
        release = threading.Event()

        def _hold() -> None:
            entered.set()
            self.assertTrue(release.wait(timeout=10), "probe never released")

        parked_result: dict[str, bool] = {}
        old_fake = _FakeFFmpeg(init_returncode=0, on_init=_hold)
        with _Patched(old_fake):
            parked = threading.Thread(
                target=lambda: parked_result.__setitem__(
                    "old", ff.nvenc_available("h264_nvenc")))
            parked.start()
            self.assertTrue(entered.wait(timeout=10), "probe never started")

            old_gen = ff.current_probe_generation()
            ff.abort_hardware_probes()
            ff.clear_hardware_probe_abort()
            self.assertNotEqual(ff.current_probe_generation(), old_gen)

            # The new generation is not blocked behind the parked probe's
            # lifecycle state and publishes its own result.
            fresh = _FakeFFmpeg(init_returncode=0)
            with _Patched(fresh):
                self.assertTrue(ff.amf_available("h264_amf"))
            self.assertIs(ff._encoder_cache["h264_amf"], True)

            release.set()
            parked.join(timeout=10)

        self.assertFalse(parked.is_alive())
        self.assertIs(parked_result["old"], False)
        self.assertNotIn("h264_nvenc", ff._encoder_cache)

    # --- T6: abort does not un-learn a fact already published -------------

    def test_t6_abort_after_a_valid_commit_keeps_the_result(self) -> None:
        """Policy lock-in: abort stops outstanding work, it does not erase
        capability knowledge observed before the cancellation."""
        fake = _FakeFFmpeg(init_returncode=0)
        with _Patched(fake):
            self.assertTrue(ff.nvenc_available("h264_nvenc"))
        self.assertIs(ff._encoder_cache["h264_nvenc"], True)

        ff.abort_hardware_probes()
        self.assertIs(ff._encoder_cache["h264_nvenc"], True)

        after = _FakeFFmpeg(init_returncode=1)
        with _Patched(after):
            self.assertTrue(ff.nvenc_available("h264_nvenc"))
        self.assertEqual(after.calls, [])

    # --- T7: coalescing survives the redesign ------------------------------

    def test_t7_concurrent_callers_for_one_encoder_probe_exactly_once(
        self,
    ) -> None:
        entered = threading.Event()
        release = threading.Event()

        def _hold() -> None:
            entered.set()
            self.assertTrue(release.wait(timeout=10), "probe never released")

        fake = _FakeFFmpeg(init_returncode=0, on_init=_hold)
        results: dict[str, bool] = {}

        def _ask(tag: str) -> None:
            results[tag] = ff.nvenc_available("h264_nvenc")

        with _Patched(fake):
            first = threading.Thread(target=_ask, args=("a",))
            first.start()
            self.assertTrue(entered.wait(timeout=10), "first probe never ran")
            second = threading.Thread(target=_ask, args=("b",))
            second.start()
            release.set()
            first.join(timeout=10)
            second.join(timeout=10)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(results, {"a": True, "b": True})
        self.assertEqual(len(fake.init_calls), 1)
        self.assertEqual(list(ff._encoder_cache.items()), [("h264_nvenc", True)])


# --- Group L: structural lock discipline ----------------------------------
#
# Two invariants cannot be exercised at runtime without deliberately building
# a deadlock, so they are checked against the parsed module instead: the
# lifecycle lock is the only state owner, and nothing expensive or
# lock-taking happens inside its critical sections.

_LIFECYCLE_LOCK = "_lifecycle_lock"
_RETIRED_LOCKS = ("_cache_lock", "_probe_procs_lock", "_aborted_generations")
# Anything that blocks, spawns, touches a child process, or takes another
# lock. None of it may appear inside a lifecycle critical section.
_FORBIDDEN_IN_CRITICAL_SECTION = frozenset({
    "Popen", "communicate", "wait", "kill", "terminate", "run", "acquire",
    "require_ffmpeg", "_run_probe", "_probe_hardware_encoder",
    "_ffmpeg_encoder_listing", "hardware_encoder_available",
    "hardware_probes_aborted", "current_probe_generation",
    "abort_hardware_probes", "clear_hardware_probe_abort",
    "_coalescing_lock", "_cached_encoder", "_cached_listing",
    "_commit_encoder_result", "_commit_listing",
})
# The only functions allowed to write the shared caches.
_CACHE_WRITERS = frozenset({
    "_commit_encoder_result", "_commit_listing", "reset_hardware_encoder_cache",
})


def _called_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _is_lifecycle_with(node: ast.With) -> bool:
    return any(
        isinstance(item.context_expr, ast.Name)
        and item.context_expr.id == _LIFECYCLE_LOCK
        for item in node.items
    )


class GroupLLockDiscipline(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = Path(ff.__file__).read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.source)

    def test_l1_the_retired_locks_are_gone(self) -> None:
        """One state owner is the point of the design: no parallel lock may
        be kept around 'for safety'."""
        names = {
            n.id for n in ast.walk(self.tree) if isinstance(n, ast.Name)
        } | {
            n.attr for n in ast.walk(self.tree) if isinstance(n, ast.Attribute)
        }
        for retired in _RETIRED_LOCKS:
            self.assertNotIn(retired, names)

    def test_l2_lifecycle_state_is_owned_by_the_lifecycle_lock(self) -> None:
        lifecycle_withs = [
            n for n in ast.walk(self.tree)
            if isinstance(n, ast.With) and _is_lifecycle_with(n)
        ]
        self.assertTrue(lifecycle_withs)
        # Every `with` in the module that guards shared state is this lock.
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.With):
                continue
            for item in node.items:
                expr = item.context_expr
                if isinstance(expr, ast.Name) and expr.id.endswith("_lock"):
                    self.assertEqual(expr.id, _LIFECYCLE_LOCK)

    def test_l3_no_lifecycle_critical_section_does_expensive_or_locking_work(
        self,
    ) -> None:
        for node in ast.walk(self.tree):
            if not (isinstance(node, ast.With) and _is_lifecycle_with(node)):
                continue
            for inner in ast.walk(node):
                if inner is node:
                    continue
                self.assertNotIsInstance(
                    inner, ast.With,
                    "a lifecycle critical section takes a second lock")
                if isinstance(inner, ast.Call):
                    self.assertNotIn(
                        _called_name(inner), _FORBIDDEN_IN_CRITICAL_SECTION,
                        f"{_called_name(inner)}() inside the lifecycle lock")

    def test_l4_the_shared_caches_are_written_only_by_the_commit_helpers(
        self,
    ) -> None:
        for func in ast.walk(self.tree):
            if not isinstance(func, ast.FunctionDef):
                continue
            if func.name in _CACHE_WRITERS:
                continue
            for node in ast.walk(func):
                targets: list[ast.expr] = []
                if isinstance(node, ast.Assign):
                    targets = list(node.targets)
                elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                    targets = [node.target]
                for target in targets:
                    base = target
                    while isinstance(base, ast.Subscript):
                        base = base.value
                    if isinstance(base, ast.Name):
                        self.assertNotIn(
                            base.id, ("_encoder_cache", "_encoder_listing"),
                            f"{func.name} writes the shared cache directly")

    def test_l5_the_coalescing_lock_is_taken_outside_the_lifecycle_lock(
        self,
    ) -> None:
        """The only nesting that may exist is coalescing -> lifecycle. The
        table lookup returns under the lifecycle lock; the lock it returns
        is taken after that section closed."""
        helper = next(
            n for n in ast.walk(self.tree)
            if isinstance(n, ast.FunctionDef) and n.name == "_coalescing_lock"
        )
        withs = [n for n in ast.walk(helper) if isinstance(n, ast.With)]
        self.assertEqual(len(withs), 1)
        self.assertTrue(_is_lifecycle_with(withs[0]))
        # And the caller takes it with no lifecycle state held.
        caller = next(
            n for n in ast.walk(self.tree)
            if isinstance(n, ast.FunctionDef)
            and n.name == "hardware_encoder_available"
        )
        for node in ast.walk(caller):
            if isinstance(node, ast.With):
                self.assertFalse(_is_lifecycle_with(node))


if __name__ == "__main__":
    unittest.main()
