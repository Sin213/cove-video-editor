"""Per-item volume on AddedAudio: default, clone and split preservation."""
from pathlib import Path
import unittest

from cove_video_editor.clip import AddedAudio, split_added_audio


def _added(**kwargs) -> AddedAudio:
    opts = dict(path=Path("added.mp3"), duration=10.0, rate=48000, offset=2.0, lane=1)
    opts.update(kwargs)
    return AddedAudio(**opts)


class AddedAudioVolumeModelTests(unittest.TestCase):
    def test_new_added_audio_defaults_to_full_volume(self) -> None:
        self.assertEqual(_added().volume, 1.0)

    def test_added_audio_accepts_custom_volume(self) -> None:
        self.assertEqual(_added(volume=0.25).volume, 0.25)

    def test_clone_preserves_half_volume(self) -> None:
        self.assertEqual(_added(volume=0.5).clone().volume, 0.5)

    def test_clone_preserves_boosted_volume(self) -> None:
        self.assertEqual(_added(volume=1.5).clone().volume, 1.5)

    def test_clone_preserves_existing_fields(self) -> None:
        a = _added(volume=0.75, src_start=1.0, src_end=8.0, peaks=[0.1, 0.2])
        c = a.clone()

        self.assertEqual(c.path, a.path)
        self.assertEqual(c.id, a.id)
        self.assertEqual(c.duration, a.duration)
        self.assertEqual(c.rate, a.rate)
        self.assertEqual(c.peaks, a.peaks)
        self.assertEqual(c.offset, a.offset)
        self.assertEqual(c.lane, a.lane)
        self.assertEqual(c.src_start, a.src_start)
        self.assertEqual(c.src_end, a.src_end)

    def test_split_preserves_volume_on_first_half(self) -> None:
        a = _added(volume=0.5)
        right = split_added_audio(a, 6.0)

        self.assertIsNotNone(right)
        self.assertEqual(a.volume, 0.5)

    def test_split_preserves_volume_on_second_half(self) -> None:
        a = _added(volume=0.5)
        right = split_added_audio(a, 6.0)

        self.assertIsNotNone(right)
        self.assertEqual(right.volume, 0.5)

    def test_split_preserves_zero_volume_on_both_halves(self) -> None:
        a = _added(volume=0.0)
        right = split_added_audio(a, 6.0)

        self.assertIsNotNone(right)
        self.assertEqual(a.volume, 0.0)
        self.assertEqual(right.volume, 0.0)


if __name__ == "__main__":
    unittest.main()
