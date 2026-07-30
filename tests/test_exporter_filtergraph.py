from pathlib import Path
import unittest

from cove_video_editor.clip import Clip, MediaAsset
from cove_video_editor.exporter import (
    AudioTrack,
    ExportJob,
    ExportWorker,
    _join_filter_labels,
    resolve_target_size,
)


def _asset(
    name: str,
    *,
    has_audio: bool,
    kind: str = "video",
    width: int = 1280,
    height: int = 720,
) -> MediaAsset:
    return MediaAsset(
        path=Path(name),
        duration=1.0,
        width=width,
        height=height,
        fps=30.0,
        has_audio=has_audio,
        kind=kind,
    )


class ExporterFiltergraphTests(unittest.TestCase):
    def test_video_concat_uses_generated_segment_labels(self) -> None:
        clips = [
            Clip(_asset("with-audio.mp4", has_audio=True), timeline_start=0.0),
            Clip(_asset("without-audio.mp4", has_audio=False), timeline_start=1.0),
            Clip(_asset("still.png", has_audio=False, kind="image"), timeline_start=2.0),
        ]
        job = ExportJob(clips=clips, output=Path("out.mp4"), fmt_key="mp4")
        worker = ExportWorker(job)

        graph, v_label, a_label = worker._build_filtergraph(
            [("clip", c.timeline_start, c.timeline_end, c) for c in clips],
            {c.id: i for i, c in enumerate(clips)},
            [],
            tgt_w=1280,
            tgt_h=720,
            is_audio_only=False,
            needs_audio=True,
        )

        concat_line = next(part for part in graph.split(";") if "concat=n=3" in part)
        # Inputs must be interleaved per segment (v0,a0,v1,a1,...) as required
        # by the ffmpeg concat filter. All-video-then-all-audio causes type-mismatch.
        self.assertEqual(
            concat_line,
            "[v0][a0][v1][a1][v2][a2]concat=n=3:v=1:a=1[vc][ac]",
        )
        self.assertNotIn("[0:v][1:v][2]", concat_line)
        self.assertEqual(v_label, "vc")
        self.assertEqual(a_label, "ac")

    def test_mixed_resolution_video_branches_are_normalized(self) -> None:
        """Mixed-resolution sources must reach concat with matching SAR and
        pixel format, not just matching dimensions."""
        clips = [
            Clip(_asset("hd.mp4", has_audio=True, width=1920, height=1080),
                 timeline_start=0.0),
            Clip(_asset("wxga.mp4", has_audio=True, width=1366, height=768),
                 timeline_start=1.0),
        ]
        job = ExportJob(clips=clips, output=Path("out.mp4"), fmt_key="MP4 (H.264 + AAC)")
        worker = ExportWorker(job)

        graph, _, _ = worker._build_filtergraph(
            [("clip", c.timeline_start, c.timeline_end, c) for c in clips],
            {c.id: i for i, c in enumerate(clips)},
            [],
            tgt_w=1920, tgt_h=1080,
            is_audio_only=False, needs_audio=True,
        )

        v_parts = [p for p in graph.split(";") if p.endswith("[v0]") or p.endswith("[v1]")]
        self.assertEqual(len(v_parts), 2)
        for part in v_parts:
            self.assertIn("scale=1920:1080:force_original_aspect_ratio=decrease"
                          ":force_divisible_by=2", part)
            self.assertIn("pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=black", part)
            self.assertIn("setsar=1", part)
            self.assertIn("format=yuv420p", part)
        self.assertIn("[v0][a0][v1][a1]concat=n=2:v=1:a=1[vc][ac]", graph)

    def test_image_and_video_branches_both_normalized(self) -> None:
        clips = [
            Clip(_asset("hd.mp4", has_audio=True, width=1920, height=1080),
                 timeline_start=0.0),
            Clip(_asset("card.png", has_audio=False, kind="image",
                        width=800, height=600), timeline_start=1.0),
        ]
        job = ExportJob(clips=clips, output=Path("out.mp4"), fmt_key="MP4 (H.264 + AAC)")
        worker = ExportWorker(job)

        graph, _, _ = worker._build_filtergraph(
            [("clip", c.timeline_start, c.timeline_end, c) for c in clips],
            {c.id: i for i, c in enumerate(clips)},
            [],
            tgt_w=1920, tgt_h=1080,
            is_audio_only=False, needs_audio=True,
        )

        img = next(p for p in graph.split(";") if p.endswith("[v1]"))
        self.assertIn("setpts=PTS-STARTPTS", img)
        self.assertNotIn("trim=start=", img)
        self.assertIn("scale=1920:1080:force_original_aspect_ratio=decrease"
                      ":force_divisible_by=2", img)
        self.assertIn("pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=black", img)
        self.assertIn("setsar=1", img)
        self.assertIn("format=yuv420p", img)

    def test_gap_visual_branch_is_normalized(self) -> None:
        clip = Clip(_asset("v.mp4", has_audio=True, width=1920, height=1080),
                    timeline_start=1.0)
        job = ExportJob(clips=[clip], output=Path("out.mp4"), fmt_key="MP4 (H.264 + AAC)")
        worker = ExportWorker(job)

        graph, _, _ = worker._build_filtergraph(
            [("gap", 0.0, 1.0, None), ("clip", 1.0, 2.0, clip)],
            {clip.id: 0},
            [],
            tgt_w=1920, tgt_h=1080,
            is_audio_only=False, needs_audio=True,
        )

        gap = next(p for p in graph.split(";") if p.endswith("[v0]"))
        self.assertIn("color=c=black:s=1920x1080", gap)
        self.assertIn("setsar=1", gap)
        self.assertIn("format=yuv420p", gap)

    def test_same_resolution_export_keeps_normalization_and_shape(self) -> None:
        clips = [
            Clip(_asset("a.mp4", has_audio=True), timeline_start=0.0),
            Clip(_asset("b.mp4", has_audio=True), timeline_start=1.0),
        ]
        job = ExportJob(clips=clips, output=Path("out.mp4"), fmt_key="MP4 (H.264 + AAC)")
        worker = ExportWorker(job)

        graph, v_label, a_label = worker._build_filtergraph(
            [("clip", c.timeline_start, c.timeline_end, c) for c in clips],
            {c.id: i for i, c in enumerate(clips)},
            [],
            tgt_w=1280, tgt_h=720,
            is_audio_only=False, needs_audio=True,
        )

        self.assertEqual((v_label, a_label), ("vc", "ac"))
        self.assertIn("[v0][a0][v1][a1]concat=n=2:v=1:a=1[vc][ac]", graph)
        for part in [p for p in graph.split(";") if p.endswith("[v0]") or p.endswith("[v1]")]:
            self.assertIn("scale=1280:720:force_original_aspect_ratio=decrease"
                          ":force_divisible_by=2", part)
            self.assertIn("setsar=1", part)
            self.assertIn("format=yuv420p", part)

    def test_concat_label_join_rejects_raw_input_labels(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "invalid concat label"):
            _join_filter_labels(["v0", "2"])
        with self.assertRaisesRegex(RuntimeError, "invalid concat label"):
            _join_filter_labels(["v0", "0:v"])

    def test_image_clip_silence_uses_48k_by_default(self) -> None:
        """anullsrc defaults to 48000 Hz for AAC/Opus targets."""
        clip = Clip(_asset("still.jpg", has_audio=False, kind="image"), timeline_start=0.0)
        clip.src_end = 3.0
        job = ExportJob(clips=[clip], output=Path("out.mp4"), fmt_key="MP4 (H.264 + AAC)")
        worker = ExportWorker(job)
        graph, _, _ = worker._build_filtergraph(
            [("clip", 0.0, 3.0, clip)],
            {clip.id: 0},
            [],
            tgt_w=1280, tgt_h=720,
            is_audio_only=False, needs_audio=True,
        )
        self.assertIn("sample_rate=48000", graph)
        self.assertNotIn("aformat", graph)

    def test_image_clip_silence_uses_44100_for_mp3(self) -> None:
        """anullsrc uses 44100 Hz + aformat when target codec is libmp3lame."""
        clip = Clip(_asset("still.jpg", has_audio=False, kind="image"), timeline_start=0.0)
        clip.src_end = 3.0
        job = ExportJob(clips=[clip], output=Path("out.avi"), fmt_key="AVI (MPEG-4 + MP3)")
        worker = ExportWorker(job)
        graph, _, _ = worker._build_filtergraph(
            [("clip", 0.0, 3.0, clip)],
            {clip.id: 0},
            [],
            tgt_w=1280, tgt_h=720,
            is_audio_only=False, needs_audio=True,
            acodec="libmp3lame",
        )
        self.assertIn("sample_rate=44100", graph)
        self.assertIn("aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo", graph)

    def test_gap_silence_uses_44100_for_mp3(self) -> None:
        """Gap segments also use 44100 Hz silence for libmp3lame."""
        clip = Clip(_asset("v.mp4", has_audio=True), timeline_start=1.0)
        clip.src_end = 1.0
        job = ExportJob(clips=[clip], output=Path("out.avi"), fmt_key="AVI (MPEG-4 + MP3)")
        worker = ExportWorker(job)
        graph, _, _ = worker._build_filtergraph(
            [("gap", 0.0, 1.0, None), ("clip", 1.0, 2.0, clip)],
            {clip.id: 0},
            [],
            tgt_w=1280, tgt_h=720,
            is_audio_only=False, needs_audio=True,
            acodec="libmp3lame",
        )
        self.assertIn("sample_rate=44100", graph)

    def test_default_clip_volume_does_not_add_filter(self) -> None:
        clip = Clip(_asset("v.mp4", has_audio=True), timeline_start=0.0)
        job = ExportJob(clips=[clip], output=Path("out.mp4"), fmt_key="MP4 (H.264 + AAC)")
        worker = ExportWorker(job)

        graph, _, _ = worker._build_filtergraph(
            [("clip", 0.0, 1.0, clip)],
            {clip.id: 0},
            [],
            tgt_w=1280,
            tgt_h=720,
            is_audio_only=False,
            needs_audio=True,
        )

        self.assertNotIn("volume=", graph)
        self.assertIn("[0:a]atrim=start=0.000:end=1.000,asetpts=PTS-STARTPTS[a0]", graph)

    def test_clip_volume_adds_ffmpeg_volume_filter(self) -> None:
        clip = Clip(_asset("v.mp4", has_audio=True), timeline_start=0.0)
        clip.audio_volume = 1.5
        job = ExportJob(clips=[clip], output=Path("out.mp4"), fmt_key="MP4 (H.264 + AAC)")
        worker = ExportWorker(job)

        graph, _, _ = worker._build_filtergraph(
            [("clip", 0.0, 1.0, clip)],
            {clip.id: 0},
            [],
            tgt_w=1280,
            tgt_h=720,
            is_audio_only=False,
            needs_audio=True,
        )

        self.assertIn(
            "[0:a]atrim=start=0.000:end=1.000,asetpts=PTS-STARTPTS,volume=1.500[a0]",
            graph,
        )

    def test_zero_clip_volume_exports_silence_filter(self) -> None:
        clip = Clip(_asset("v.mp4", has_audio=True), timeline_start=0.0)
        clip.audio_volume = 0.0
        job = ExportJob(clips=[clip], output=Path("out.mp4"), fmt_key="MP4 (H.264 + AAC)")
        worker = ExportWorker(job)

        graph, _, _ = worker._build_filtergraph(
            [("clip", 0.0, 1.0, clip)],
            {clip.id: 0},
            [],
            tgt_w=1280,
            tgt_h=720,
            is_audio_only=False,
            needs_audio=True,
        )

        self.assertIn("volume=0.000[a0]", graph)

    def test_no_audio_clip_uses_generated_silence_for_export_audio(self) -> None:
        clip = Clip(_asset("silent.mp4", has_audio=False), timeline_start=0.0)
        job = ExportJob(clips=[clip], output=Path("out.mp4"), fmt_key="MP4 (H.264 + AAC)")
        worker = ExportWorker(job)

        graph, _, a_label = worker._build_filtergraph(
            [("clip", 0.0, 1.0, clip)],
            {clip.id: 0},
            [],
            tgt_w=1280,
            tgt_h=720,
            is_audio_only=False,
            needs_audio=True,
        )

        self.assertEqual(a_label, "ac")
        self.assertNotIn("[0:a]", graph)
        self.assertIn("anullsrc=channel_layout=stereo:sample_rate=48000", graph)

    def test_audio_only_export_builds_command_with_no_video_clips(self) -> None:
        """Standalone added-audio (no video clips) can build an audio-only
        command: no video map/codec, and an audio map to the output path."""
        track = AudioTrack(path=Path("added.mp3"), offset=0.0, duration=2.0)
        job = ExportJob(
            clips=[], output=Path("out.wav"), fmt_key="WAV (audio only)",
            audio_tracks=[track],
        )
        worker = ExportWorker(job)

        cmd = worker._build_command()

        self.assertNotIn("-c:v", cmd)
        self.assertEqual(cmd.count("-map"), 1)
        self.assertIn("-c:a", cmd)
        self.assertIn("pcm_s16le", cmd)
        self.assertEqual(cmd[-1], "out.wav")

    def test_project_export_still_rejects_no_clips(self) -> None:
        """Project/video export must keep rejecting empty timelines, even
        when added-audio tracks are present."""
        track = AudioTrack(path=Path("added.mp3"), offset=0.0, duration=2.0)
        job = ExportJob(
            clips=[], output=Path("out.mp4"), fmt_key="MP4 (H.264 + AAC)",
            audio_tracks=[track],
        )
        worker = ExportWorker(job)

        with self.assertRaisesRegex(RuntimeError, "no clips to export"):
            worker._build_command()


def _filter_complex(cmd: list[str]) -> str:
    return cmd[cmd.index("-filter_complex") + 1]


class ResolveTargetSizeTests(unittest.TestCase):
    """Pure target-size policy: crop > explicit width/height > first real
    visual clip > 1280x720, with the final dimensions forced even."""

    def test_auto_uses_first_real_clip(self) -> None:
        clips = [Clip(_asset("hd.mp4", has_audio=True, width=1920, height=1080))]
        self.assertEqual(resolve_target_size(clips, None, None, None), (1920, 1080))

    def test_auto_ignores_later_mixed_resolution_clips(self) -> None:
        clips = [
            Clip(_asset("hd.mp4", has_audio=True, width=1920, height=1080),
                 timeline_start=0.0),
            Clip(_asset("wxga.mp4", has_audio=True, width=1366, height=768),
                 timeline_start=1.0),
        ]
        self.assertEqual(resolve_target_size(clips, None, None, None), (1920, 1080))

    def test_explicit_landscape_preset_overrides_auto(self) -> None:
        clips = [Clip(_asset("hd.mp4", has_audio=True, width=1920, height=1080))]
        self.assertEqual(resolve_target_size(clips, None, 1280, 720), (1280, 720))

    def test_explicit_portrait_preset_overrides_auto(self) -> None:
        clips = [Clip(_asset("hd.mp4", has_audio=True, width=1920, height=1080))]
        self.assertEqual(resolve_target_size(clips, None, 1080, 1920), (1080, 1920))

    def test_explicit_square_preset_overrides_auto(self) -> None:
        clips = [Clip(_asset("hd.mp4", has_audio=True, width=1920, height=1080))]
        self.assertEqual(resolve_target_size(clips, None, 1080, 1080), (1080, 1080))

    def test_crop_overrides_explicit_preset(self) -> None:
        clips = [Clip(_asset("hd.mp4", has_audio=True, width=1920, height=1080))]
        self.assertEqual(
            resolve_target_size(clips, (10, 20, 640, 480), 1080, 1920), (640, 480)
        )

    def test_no_real_visual_clip_falls_back_to_720p(self) -> None:
        self.assertEqual(resolve_target_size([], None, None, None), (1280, 720))
        zero = [Clip(_asset("audio-ish.mp4", has_audio=True, width=0, height=0))]
        self.assertEqual(resolve_target_size(zero, None, None, None), (1280, 720))

    def test_odd_source_dimensions_are_forced_even(self) -> None:
        clips = [Clip(_asset("odd.mp4", has_audio=True, width=1921, height=1081))]
        self.assertEqual(resolve_target_size(clips, None, None, None), (1920, 1080))

    def test_odd_crop_dimensions_are_forced_even(self) -> None:
        clips = [Clip(_asset("hd.mp4", has_audio=True, width=1920, height=1080))]
        self.assertEqual(
            resolve_target_size(clips, (0, 0, 641, 481), None, None), (640, 480)
        )

    def test_leading_gap_does_not_define_target(self) -> None:
        """A clip starting later on the timeline still defines the target;
        the synthesized leading gap does not."""
        clips = [Clip(_asset("hd.mp4", has_audio=True, width=1920, height=1080),
                      timeline_start=5.0)]
        self.assertEqual(resolve_target_size(clips, None, None, None), (1920, 1080))

    def test_partial_explicit_size_is_ignored(self) -> None:
        clips = [Clip(_asset("hd.mp4", has_audio=True, width=1920, height=1080))]
        self.assertEqual(resolve_target_size(clips, None, 1280, None), (1920, 1080))
        self.assertEqual(resolve_target_size(clips, None, None, 720), (1920, 1080))


class ExplicitResolutionCommandTests(unittest.TestCase):
    def _mixed_clips(self) -> list[Clip]:
        return [
            Clip(_asset("hd.mp4", has_audio=True, width=1920, height=1080),
                 timeline_start=0.0),
            Clip(_asset("wxga.mp4", has_audio=True, width=1366, height=768),
                 timeline_start=1.0),
        ]

    def _graph_for(self, width: int | None, height: int | None) -> str:
        job = ExportJob(
            clips=self._mixed_clips(), output=Path("out.mp4"),
            fmt_key="MP4 (H.264 + AAC)", width=width, height=height,
        )
        return _filter_complex(ExportWorker(job)._build_command())

    def _assert_targets(self, graph: str, w: int, h: int) -> None:
        v_parts = [p for p in graph.split(";")
                   if p.endswith("[v0]") or p.endswith("[v1]")]
        self.assertEqual(len(v_parts), 2)
        for part in v_parts:
            self.assertIn(
                f"scale={w}:{h}:force_original_aspect_ratio=decrease"
                ":force_divisible_by=2", part,
            )
            self.assertIn(f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black", part)
            self.assertIn("setsar=1", part)
            self.assertIn("format=yuv420p", part)

    def test_explicit_720p_targets_1280x720(self) -> None:
        self._assert_targets(self._graph_for(1280, 720), 1280, 720)

    def test_explicit_portrait_targets_1080x1920(self) -> None:
        self._assert_targets(self._graph_for(1080, 1920), 1080, 1920)

    def test_explicit_square_targets_1080x1080(self) -> None:
        self._assert_targets(self._graph_for(1080, 1080), 1080, 1080)

    def test_auto_still_targets_first_real_clip(self) -> None:
        self._assert_targets(self._graph_for(None, None), 1920, 1080)

    def test_audio_only_command_ignores_width_and_height(self) -> None:
        track = AudioTrack(path=Path("added.mp3"), offset=0.0, duration=2.0)
        job = ExportJob(
            clips=[], output=Path("out.wav"), fmt_key="WAV (audio only)",
            audio_tracks=[track], width=1080, height=1920,
        )
        cmd = ExportWorker(job)._build_command()
        graph = _filter_complex(cmd)

        self.assertNotIn("-c:v", cmd)
        self.assertEqual(cmd.count("-map"), 1)
        self.assertNotIn("scale=", graph)
        self.assertNotIn("1080:1920", graph)
        self.assertEqual(cmd[-1], "out.wav")


if __name__ == "__main__":
    unittest.main()
