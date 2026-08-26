"""Tab 2D: clicking empty media-bin space asks the app to browse.

Real widgets on the ``offscreen`` platform plugin, driven with real
`QMouseEvent`s through `QApplication.sendEvent`, so `itemAt()` and the
press/release bookkeeping are exercised rather than mocked. The widget
only reports intent - opening the picker is `app.py`'s job - so these
tests assert on the emitted `browseRequested` signal.
"""
from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QListWidgetItem  # noqa: E402

from cove_video_editor.clip_bin import BROWSE_CLICK_SLOP_PX, AssetList, ClipBin  # noqa: E402


_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


def _send(widget, kind, pos: QPoint, button=Qt.LeftButton) -> None:
    ev = QMouseEvent(
        kind, QPointF(pos), QPointF(pos), button, button, Qt.NoModifier,
    )
    QApplication.sendEvent(widget.viewport(), ev)


class _Spy:
    def __init__(self, signal) -> None:
        self.count = 0
        signal.connect(self._hit)

    def _hit(self, *args) -> None:
        self.count += 1


class ClickToBrowseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.lst = AssetList()
        self.lst.resize(400, 300)
        self.spy = _Spy(self.lst.browseRequested)
        # Well clear of any item cell (grid is 140x106 at the top-left).
        self.empty = QPoint(300, 260)

    def tearDown(self) -> None:
        self.lst.deleteLater()

    def _add_item(self) -> QListWidgetItem:
        item = QListWidgetItem("clip.mp4")
        item.setData(Qt.UserRole, "asset-1")
        self.lst.addItem(item)
        return item

    def test_g1_plain_click_on_empty_space_browses_once(self) -> None:
        _send(self.lst, QMouseEvent.Type.MouseButtonPress, self.empty)
        _send(self.lst, QMouseEvent.Type.MouseButtonRelease, self.empty)
        self.assertEqual(self.spy.count, 1)

    def test_g2_jitter_below_threshold_still_browses(self) -> None:
        jitter = QPoint(BROWSE_CLICK_SLOP_PX - 1, 0)
        _send(self.lst, QMouseEvent.Type.MouseButtonPress, self.empty)
        _send(self.lst, QMouseEvent.Type.MouseButtonRelease, self.empty + jitter)
        self.assertEqual(self.spy.count, 1)

    def test_g3_movement_above_threshold_does_not_browse(self) -> None:
        drag = QPoint(BROWSE_CLICK_SLOP_PX + 20, BROWSE_CLICK_SLOP_PX + 20)
        _send(self.lst, QMouseEvent.Type.MouseButtonPress, self.empty)
        _send(self.lst, QMouseEvent.Type.MouseButtonRelease, self.empty - drag)
        self.assertEqual(self.spy.count, 0)

    def test_g3b_exactly_at_threshold_does_not_browse(self) -> None:
        drag = QPoint(BROWSE_CLICK_SLOP_PX, 0)
        _send(self.lst, QMouseEvent.Type.MouseButtonPress, self.empty)
        _send(self.lst, QMouseEvent.Type.MouseButtonRelease, self.empty + drag)
        self.assertEqual(self.spy.count, 0)

    def test_g4_click_on_an_existing_item_does_not_browse(self) -> None:
        item = self._add_item()
        pos = self.lst.visualItemRect(item).center()
        self.assertIsNotNone(self.lst.itemAt(pos), "fixture: item must be hit-testable")
        _send(self.lst, QMouseEvent.Type.MouseButtonPress, pos)
        _send(self.lst, QMouseEvent.Type.MouseButtonRelease, pos)
        self.assertEqual(self.spy.count, 0)

    def test_g4b_press_on_item_release_on_empty_does_not_browse(self) -> None:
        item = self._add_item()
        pos = self.lst.visualItemRect(item).center()
        _send(self.lst, QMouseEvent.Type.MouseButtonPress, pos)
        _send(self.lst, QMouseEvent.Type.MouseButtonRelease, self.empty)
        self.assertEqual(self.spy.count, 0)

    def test_g5_right_click_does_not_browse(self) -> None:
        _send(self.lst, QMouseEvent.Type.MouseButtonPress, self.empty, Qt.RightButton)
        _send(self.lst, QMouseEvent.Type.MouseButtonRelease, self.empty, Qt.RightButton)
        self.assertEqual(self.spy.count, 0)

    def test_g6_release_without_a_matching_press_does_not_browse(self) -> None:
        _send(self.lst, QMouseEvent.Type.MouseButtonRelease, self.empty)
        self.assertEqual(self.spy.count, 0)

    def test_g6b_second_release_does_not_browse_again(self) -> None:
        _send(self.lst, QMouseEvent.Type.MouseButtonPress, self.empty)
        _send(self.lst, QMouseEvent.Type.MouseButtonRelease, self.empty)
        _send(self.lst, QMouseEvent.Type.MouseButtonRelease, self.empty)
        self.assertEqual(self.spy.count, 1)


class ClipBinBrowseKindTests(unittest.TestCase):
    """The panel forwards each tab's browse request with its media kind."""

    def setUp(self) -> None:
        self.bin = ClipBin()
        self.seen: list[str] = []
        self.bin.browseRequested.connect(self.seen.append)

    def tearDown(self) -> None:
        self.bin.deleteLater()

    def test_each_tab_reports_its_own_kind(self) -> None:
        for lst, kind in (
            (self.bin.video_list, "video"),
            (self.bin.audio_list, "audio"),
            (self.bin.image_list, "image"),
            (self.bin.subs_list, "sub"),
        ):
            lst.browseRequested.emit()
        self.assertEqual(self.seen, ["video", "audio", "image", "sub"])


class EmptyStateHintTests(unittest.TestCase):
    """The empty state must advertise both ways in."""

    def test_hint_mentions_clicking_and_dropping(self) -> None:
        lines = AssetList.EMPTY_HINT
        self.assertEqual(len(lines), 2)
        self.assertIn("click", lines[0].lower())
        self.assertIn("drop", lines[1].lower())
        self.assertIn("folder", lines[1].lower())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
