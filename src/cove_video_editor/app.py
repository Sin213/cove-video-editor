from __future__ import annotations

import copy
import os
import time
from pathlib import Path

from PySide6.QtCore import (
    QEvent,
    QPoint,
    QRectF,
    QSizeF,
    QStandardPaths,
    QThread,
    QTimer,
    QUrl,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QDesktopServices,
    QDragEnterEvent,
    QDropEvent,
    QFont,
    QFontDatabase,
    QIcon,
    QImage,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QShortcut,
)
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QGraphicsVideoItem
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGraphicsItemGroup,
    QGraphicsPathItem,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QProgressDialog,
    QPushButton,
    QScrollBar,
    QSizeGrip,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QSplitter,
    QStatusBar,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import __version__
from . import ffmpeg_utils as ff
from . import theme
from . import updater
from .system_open import open_url as _open_url
from .titlebar import TitleBar, FramelessResizer
from .clip import (
    DEFAULT_IMAGE_DURATION,
    IMAGE_ASSET_DURATION_CAP,
    AddedAudio,
    Clip,
    MediaAsset,
    SubtitleTrack,
    clip_at_timeline,
    delete_region,
    keep_only_region,
    parse_sub_cues,
    sequence_length,
    sort_clips,
    split_added_audio,
    split_clip,
)
from .clip_bin import ASSET_MIME, ClipBin
from .crop_overlay import CropOverlay
from .downloader import DownloadVideoDialog
from .exporter import AudioTrack, ExportJob, start_export
from .thumbnails import start_thumbnails, start_waveform
from .timeline_widget import TimelineWidget


VIDEO_FILTERS = (
    "Videos (*.mp4 *.mkv *.webm *.mov *.avi *.m4v *.mpg *.mpeg *.wmv);;All files (*)"
)
AUDIO_FILTERS = "Audio (*.mp3 *.m4a *.aac *.wav *.ogg *.opus *.flac);;All files (*)"
IMAGE_FILTERS = "Images (*.png *.jpg *.jpeg *.bmp *.gif *.webp *.tiff *.tif);;All files (*)"
SUB_FILTERS = "Subtitles (*.srt *.vtt *.ass *.ssa);;All files (*)"
AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus", ".flac"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tiff", ".tif"}
SUB_EXTS = {".srt", ".vtt", ".ass", ".ssa"}

ASSETS_DIR = Path(__file__).resolve().parent / "assets"
ICON_PATH = ASSETS_DIR / "cove_icon.png"


def export_controls_enabled(
    has_clips: bool,
    has_added_audio: bool,
    audio_only: bool,
    exporting: bool,
) -> bool:
    """Whether the Export button / format combo should be enabled.

    Project export needs at least one video clip; Audio Only export also
    accepts a timeline with only AddedAudio items. Always disabled while
    an export is running.
    """
    if exporting:
        return False
    if audio_only:
        return has_clips or has_added_audio
    return has_clips


# Two computed timeline positions this close together are the same cut point.
# Matches the tolerance the rest of the timeline code uses for clip edges.
_CUT_EPS = 1e-4


def plan_ripple_move(
    clips: list[Clip],
    clip_id: str,
    target_start: float,
) -> dict[str, float] | None:
    """Work out where every visual clip lands when `clip_id` is sent to
    `target_start`, or return None when nothing would change.

    Pure position math, deliberately split out of the widget/undo wiring so
    the ordering rules can be tested without a window.

    The single video track cannot hold overlapping clips, so `target_start`
    is resolved to the nearest *cut point*: t=0, or an edge of one of the
    other clips once this clip has been pulled out of the sequence. The
    layout is then rebuilt in one pass:

    1. Pull the moving clip out; clips that sat after it slide left by its
       length, closing the gap it vacated.
    2. Collect the cut points of that adjusted sequence.
    3. Snap the request to the nearest cut point. On an exact tie the
       earlier (smaller) position wins, so the result never depends on
       iteration order.
    4. Open a gap of the clip's length at that cut point by pushing the
       clips at or after it back to the right, and drop the clip in.

    Clips that do not move keep their spacing relative to each other, so
    deliberate gaps elsewhere in the sequence survive the operation.
    """
    clip = next((c for c in clips if c.id == clip_id), None)
    if clip is None:
        return None
    target_start = max(0.0, target_start)
    length = clip.timeline_length
    old = clip.timeline_start

    # Step 1 + 2: the sequence with this clip pulled out, and its cut points.
    adjusted: list[tuple[Clip, float]] = []
    cut_points: list[float] = [0.0]
    for c in clips:
        if c.id == clip_id:
            continue
        ns = c.timeline_start - (length if c.timeline_start > old else 0.0)
        ns = max(0.0, ns)
        adjusted.append((c, ns))
        cut_points.append(ns)
        cut_points.append(ns + c.timeline_length)

    # Step 3: nearest cut point, earlier one on a tie.
    target = min(cut_points, key=lambda b: (abs(b - target_start), b))
    if abs(target - old) < _CUT_EPS:
        # Same cut point it already occupies, so the effective layout is
        # unchanged: no mutation and no undo entry.
        return None

    # Step 4: reopen a gap of `length` at the target and place the clip.
    plan = {clip_id: target}
    for c, ns in adjusted:
        if ns >= target - _CUT_EPS:
            ns += length
        plan[c.id] = max(0.0, ns)
    return plan


# ── Inline icon painters for transport / zoom buttons ──────────────────────
# Avoiding an external icon file keeps the install footprint small and lets
# icons inherit the current palette color on hover. The shapes match the
# stroke-style icons in the reference design.
def _paint_icon(btn: QToolButton, kind: str) -> None:
    class _IconRenderer(QWidget):
        def __init__(self, host: QToolButton, which: str) -> None:
            super().__init__(host)
            self._which = which
            self.setFixedSize(host.size())
            self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            self.raise_()

        def paintEvent(self, _ev) -> None:  # noqa: ANN001
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing, True)
            c = btn.palette().color(btn.foregroundRole())
            if not btn.isEnabled():
                c = QColor(theme.TEXT_3)
            cx, cy = self.width() / 2, self.height() / 2
            if self._which == "play":
                p.setPen(Qt.NoPen)
                p.setBrush(c)
                p.drawPolygon([
                    QPoint(int(cx - 5), int(cy - 6)),
                    QPoint(int(cx - 5), int(cy + 6)),
                    QPoint(int(cx + 7), int(cy)),
                ])
            elif self._which == "pause":
                p.setPen(Qt.NoPen); p.setBrush(c)
                p.drawRect(int(cx - 5), int(cy - 6), 3, 12)
                p.drawRect(int(cx + 2), int(cy - 6), 3, 12)
            elif self._which == "rewind":
                p.setPen(Qt.NoPen); p.setBrush(c)
                p.drawRect(int(cx - 8), int(cy - 6), 2, 12)
                p.drawPolygon([
                    QPoint(int(cx + 5), int(cy - 6)),
                    QPoint(int(cx + 5), int(cy + 6)),
                    QPoint(int(cx - 5), int(cy)),
                ])
            elif self._which == "end":
                p.setPen(Qt.NoPen); p.setBrush(c)
                p.drawRect(int(cx + 6), int(cy - 6), 2, 12)
                p.drawPolygon([
                    QPoint(int(cx - 5), int(cy - 6)),
                    QPoint(int(cx - 5), int(cy + 6)),
                    QPoint(int(cx + 5), int(cy)),
                ])
            elif self._which == "minus":
                pen = QPen(c, 1.8); p.setPen(pen)
                p.drawLine(int(cx - 5), int(cy), int(cx + 5), int(cy))
            elif self._which == "plus":
                pen = QPen(c, 1.8); p.setPen(pen)
                p.drawLine(int(cx - 5), int(cy), int(cx + 5), int(cy))
                p.drawLine(int(cx), int(cy - 5), int(cx), int(cy + 5))
            p.end()

    renderer = _IconRenderer(btn, kind)
    renderer.show()
    btn._icon_renderer = renderer  # keep a reference + for swap on toggle


def _make_transport_btn(kind: str, tooltip: str) -> QToolButton:
    btn = QToolButton()
    btn.setFixedSize(30, 28)
    btn.setCursor(Qt.PointingHandCursor)
    btn.setToolTip(tooltip)
    btn.setAutoRaise(True)
    _paint_icon(btn, kind)
    return btn


def _make_zoom_btn(kind: str, tooltip: str) -> QToolButton:
    btn = QToolButton()
    btn.setFixedSize(24, 24)
    btn.setCursor(Qt.PointingHandCursor)
    btn.setToolTip(tooltip)
    btn.setAutoRaise(True)
    _paint_icon(btn, kind)
    return btn


def _swap_btn_icon(btn: QToolButton, kind: str) -> None:
    """Re-render the embedded icon (used when Play ↔ Pause)."""
    old = getattr(btn, "_icon_renderer", None)
    if old is not None:
        old.setParent(None)
        old.deleteLater()
    _paint_icon(btn, kind)


# ── Subtitle preview: convert text into a QPainterPath of glyph outlines ──
def _build_subtitle_path(text: str, font: QFont) -> QPainterPath:
    """Build a multi-line, horizontally-centered glyph path. Each line is
    laid out on its own baseline using the font's line spacing so
    preview wrapping matches the exported burn-in."""
    from PySide6.QtGui import QFontMetricsF
    path = QPainterPath()
    fm = QFontMetricsF(font)
    line_h = fm.lineSpacing()
    ascent = fm.ascent()
    lines = text.split("\n")
    # Measure widest line so we can align each within a common box.
    widest = max((fm.horizontalAdvance(l) for l in lines), default=0.0)
    for i, line in enumerate(lines):
        if not line:
            continue
        line_w = fm.horizontalAdvance(line)
        x = (widest - line_w) / 2.0
        y = ascent + i * line_h
        path.addText(x, y, font, line)
    return path


# ── Subtitle font helpers ─────────────────────────────────────────────────
# Popular, broadly-recognized families plus safe Linux fallbacks. The
# style dialog filters this list to what's actually installed so the user
# never picks a font that renders as ".notdef" boxes.
_SUBTITLE_FONT_CANDIDATES = [
    "Arial",
    "Helvetica",
    "Liberation Sans",
    "DejaVu Sans",
    "Roboto",
    "Open Sans",
    "Noto Sans",
    "Inter",
    "Cantarell",
    "Verdana",
    "Tahoma",
    "Trebuchet MS",
    "Georgia",
    "Times New Roman",
    "Liberation Serif",
    "DejaVu Serif",
    "Courier New",
    "Liberation Mono",
    "JetBrains Mono",
    "Impact",
    "Comic Sans MS",
]


def available_subtitle_fonts() -> list[str]:
    """Return the subset of _SUBTITLE_FONT_CANDIDATES actually installed.

    Exposed at module scope so both the style dialog and export-path
    font-fallback can share the same list — keeps preview and burn-in in
    visual lockstep."""
    installed = set(QFontDatabase.families())
    picks = [name for name in _SUBTITLE_FONT_CANDIDATES if name in installed]
    # Always guarantee at least one choice — fall back to the app default.
    return picks or [QApplication.font().family()]


class VideoView(QGraphicsView):
    clicked = Signal()
    contextMenuRequestedAt = Signal(object)   # QPoint (global)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHints(QPainter.SmoothPixmapTransform)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setFrameShape(QFrame.NoFrame)
        self.setStyleSheet("background:#000; border:none;")
        self.setAlignment(Qt.AlignCenter)

        self.video_item = QGraphicsVideoItem()
        self.video_item.setSize(QSizeF(640, 360))
        self._scene.addItem(self.video_item)
        # Pixmap item shown in place of the video item when the current clip
        # is a still image. Starts hidden so the normal video path is
        # unaffected until the user adds an image clip.
        self.pixmap_item = QGraphicsPixmapItem()
        self.pixmap_item.setVisible(False)
        self.pixmap_item.setTransformationMode(Qt.SmoothTransformation)
        self._scene.addItem(self.pixmap_item)
        # Subtitle overlay — two stacked path items so the outline is
        # drawn OUTSIDE the glyphs (not straddling them the way a single
        # QGraphicsSimpleTextItem pen did, which gave letters a
        # wireframe/hollow look). Lives in scene coords (native-video
        # pixels) so on-screen size matches what libass burns in when we
        # pass `original_size=WxH` to ffmpeg's subtitles filter.
        self.sub_outline = QGraphicsPathItem()
        self.sub_outline.setZValue(20)
        self.sub_outline.setVisible(False)
        self._scene.addItem(self.sub_outline)
        self.sub_fill = QGraphicsPathItem()
        self.sub_fill.setZValue(21)
        self.sub_fill.setPen(Qt.NoPen)
        self.sub_fill.setVisible(False)
        self._scene.addItem(self.sub_fill)
        self._scene.setSceneRect(QRectF(0, 0, 640, 360))

    def video_output(self) -> QGraphicsVideoItem:
        return self.video_item

    def set_video_visible(self, visible: bool) -> None:
        self.video_item.setVisible(bool(visible))
        if visible:
            self.pixmap_item.setVisible(False)

    def show_image(self, pixmap: QPixmap, native_w: int, native_h: int) -> None:
        """Swap the preview to a still image at (`native_w`, `native_h`)."""
        self.video_item.setVisible(False)
        scaled = pixmap.scaled(
            native_w, native_h,
            Qt.IgnoreAspectRatio, Qt.SmoothTransformation,
        ) if pixmap.width() != native_w or pixmap.height() != native_h else pixmap
        self.pixmap_item.setPixmap(scaled)
        self.pixmap_item.setPos(0, 0)
        self.pixmap_item.setVisible(True)

    def hide_image(self) -> None:
        self.pixmap_item.setVisible(False)

    def set_subtitle_cue(self, text: str, style: dict) -> None:
        """Render `text` in the live subtitle overlay using `style` (see
        SubtitleStyleDialog for the dict shape). Empty `text` hides the
        overlay. Matches the export path that ships `original_size=WxH`
        to libass, so on-screen font size == exported pixel height."""
        if not text:
            self.sub_outline.setVisible(False)
            self.sub_fill.setVisible(False)
            return

        font = QFont(style.get("font_family") or "Arial")
        font.setPixelSize(max(8, int(style.get("font_size", 36))))
        font.setBold(True)
        # Match libass's default leading so multi-line preview spacing
        # lines up with the exported burn-in.
        font.setLetterSpacing(QFont.PercentageSpacing, 100.0)

        path = _build_subtitle_path(text, font)

        fill = QColor(style.get("primary_color", "#FFFFFF"))
        outline = QColor(style.get("outline_color", "#000000"))
        outline_width = max(0, int(style.get("outline", 2)))

        self.sub_fill.setPath(path)
        self.sub_fill.setBrush(QBrush(fill if fill.isValid() else QColor("white")))

        self.sub_outline.setPath(path)
        self.sub_outline.setBrush(QBrush(outline if outline.isValid() else QColor("black")))
        # Draw a 2× thick stroke, filled with the outline color, behind
        # the fill layer — the fill then covers the inner half so only
        # the outer rim of the stroke remains visible.
        pen = QPen(outline if outline.isValid() else QColor("black"),
                   outline_width * 2)
        pen.setJoinStyle(Qt.RoundJoin)
        pen.setCapStyle(Qt.RoundCap)
        self.sub_outline.setPen(pen if outline_width > 0 else Qt.NoPen)

        # Horizontal center; vertical either top-padded or bottom-padded.
        scene_rect = self._scene.sceneRect()
        br = self.sub_fill.boundingRect()
        x = scene_rect.left() + (scene_rect.width() - br.width()) / 2.0
        # 6% of the video height is a comfortable safe-margin.
        margin = max(12.0, scene_rect.height() * 0.06)
        if style.get("position", "bottom") == "top":
            y = scene_rect.top() + margin - br.top()
        else:
            y = scene_rect.bottom() - br.height() - margin - br.top()
        self.sub_fill.setPos(x, y)
        self.sub_outline.setPos(x, y)
        self.sub_fill.setVisible(True)
        self.sub_outline.setVisible(outline_width > 0)

    def hide_subtitle(self) -> None:
        self.sub_outline.setVisible(False)
        self.sub_fill.setVisible(False)

    def set_native_size(self, width: int, height: int) -> None:
        self.video_item.setSize(QSizeF(width, height))
        self._scene.setSceneRect(QRectF(0, 0, width, height))
        self._fit()

    def _fit(self) -> None:
        if not self._scene.sceneRect().isEmpty():
            self.fitInView(self._scene.sceneRect(), Qt.KeepAspectRatio)

    def resizeEvent(self, event) -> None:  # noqa: ANN001
        super().resizeEvent(event)
        self._fit()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def contextMenuEvent(self, event) -> None:  # noqa: ANN001
        self.contextMenuRequestedAt.emit(event.globalPos())
        event.accept()


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"Cove Video Editor v{__version__}")
        # Frameless with our own chrome — matches the cove-nexus /
        # cove-video-downloader window style.
        self.setWindowFlag(Qt.FramelessWindowHint, True)
        self.setAttribute(Qt.WA_TranslucentBackground, False)
        self.resize(1440, 900)
        self.setMinimumSize(1100, 720)
        if ICON_PATH.exists():
            self.setWindowIcon(QIcon(str(ICON_PATH)))
        self._resizer = FramelessResizer(self)
        # Visible SE-corner resize grip so the user has a discoverable
        # affordance to grab. FramelessResizer handles invisible edge drag.
        self._size_grip = QSizeGrip(self)
        self._size_grip.setFixedSize(16, 16)
        self._size_grip.raise_()

        self._assets: dict[str, MediaAsset] = {}
        # Source QPixmap for each image asset — loaded once on import and
        # reused for both the preview overlay and the timeline clip thumb.
        self._image_pixmaps: dict[str, QPixmap] = {}
        self._clips: list[Clip] = []
        self._thumb_threads: dict[str, QThread] = {}
        self._thumb_workers: dict[str, object] = {}
        self._wave_threads: dict[str, QThread] = {}
        self._wave_workers: dict[str, object] = {}
        # One waveform worker per added-audio entry (keyed by entry id).
        self._added_wave_threads: dict[str, QThread] = {}
        self._added_wave_workers: dict[str, object] = {}
        self._export_thread: QThread | None = None
        self._export_worker = None
        self._download_dialog: DownloadVideoDialog | None = None
        # List of added-audio clips on the mix track; each has its own player.
        self._added_audios: list[AddedAudio] = []
        self._added_players: dict[str, QMediaPlayer] = {}
        self._added_outputs: dict[str, QAudioOutput] = {}
        self._preview_clip_id: str = ""
        self._region_export_range: tuple[float, float] | None = None
        # Subtitle library. Zero or more loaded SRT/VTT files; at most one
        # may be `active` and gets burned in on export.
        self._subs: list[SubtitleTrack] = []

        # Undo / redo stacks — each entry is a full state snapshot. A new
        # snapshot (user action) clears the redo stack, same as Photoshop.
        self._undo_stack: list[dict] = []
        self._redo_stack: list[dict] = []
        self._undo_limit: int = 80

        self._build_ui()
        self._install_shortcuts()
        self._update_controls_enabled()
        self._check_ffmpeg()
        self.setAcceptDrops(True)
        # Update checker plumbing — populated on demand.
        self._update_thread: QThread | None = None
        self._update_worker = None
        self._update_download_thread: QThread | None = None
        self._update_download_worker = None
        self._update_prompt_shown = False
        # Kick off the first check a moment after the UI comes up so the
        # user doesn't notice any startup hitch.
        QTimer.singleShot(4000, self._check_for_updates_in_background)

    # --- frameless window helpers -------------------------------------

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        # Reposition the SE-corner QSizeGrip on every resize so it stays
        # pinned to the bottom-right of the window.
        s = self._size_grip.sizeHint()
        self._size_grip.move(self.width() - s.width(), self.height() - s.height())

    def _toggle_maximize(self) -> None:
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def changeEvent(self, event) -> None:  # noqa: ANN001
        if event.type() == QEvent.WindowStateChange:
            if hasattr(self, "titlebar"):
                self.titlebar.set_maximized(self.isMaximized())
        super().changeEvent(event)

    # --- shortcuts -----------------------------------------------------

    def _install_shortcuts(self) -> None:
        QShortcut(QKeySequence.Undo, self).activated.connect(self._undo)
        QShortcut(QKeySequence.Redo, self).activated.connect(self._redo)
        # Ctrl+Y as an explicit redo binding — QKeySequence.Redo is
        # Ctrl+Shift+Z on some platforms, Ctrl+Y on others.
        QShortcut(QKeySequence("Ctrl+Y"), self).activated.connect(self._redo)
        space = QShortcut(QKeySequence(Qt.Key_Space), self)
        space.setContext(Qt.ApplicationShortcut)
        space.activated.connect(self._toggle_play)
        # Frame step.
        QShortcut(QKeySequence(Qt.Key_Period), self).activated.connect(self._next_frame)
        QShortcut(QKeySequence(Qt.Key_Comma), self).activated.connect(self._prev_frame)
        # Jump to clip edges / sequence ends.
        QShortcut(QKeySequence(Qt.Key_Home), self).activated.connect(
            lambda: self.timeline.set_playhead(0.0),
        )
        QShortcut(QKeySequence(Qt.Key_End), self).activated.connect(
            lambda: self.timeline.set_playhead(self._total_playback_length()),
        )
        QShortcut(QKeySequence(Qt.Key_BracketLeft), self).activated.connect(self._jump_selected_clip_start)
        QShortcut(QKeySequence(Qt.Key_BracketRight), self).activated.connect(self._jump_selected_clip_end)
        QShortcut(QKeySequence("Alt+,"), self).activated.connect(self._jump_prev_clip_edge)
        QShortcut(QKeySequence("Alt+."), self).activated.connect(self._jump_next_clip_edge)
        # Merge adjacent clips (M = merge right, Shift+M = merge left).
        QShortcut(QKeySequence(Qt.Key_M), self).activated.connect(self._merge_with_next_clip)
        QShortcut(QKeySequence("Shift+M"), self).activated.connect(self._merge_with_previous_clip)
        # Esc cancels crop mode.
        esc = QShortcut(QKeySequence(Qt.Key_Escape), self)
        esc.activated.connect(self._on_escape_pressed)

    # --- scrollbar glue ------------------------------------------------

    def _on_timeline_scroll_range(self, max_px: int, page_px: int) -> None:
        sb = self.timeline_scrollbar
        sb.blockSignals(True)
        sb.setRange(0, max(0, max_px))
        sb.setPageStep(max(1, page_px))
        sb.setSingleStep(40)
        sb.blockSignals(False)
        # Always visible — disabled (grayed out) when there's nothing to
        # scroll, so the zoom cluster stays pinned right in the row
        # instead of stretching across an empty gap. VideoPad does the same.
        sb.setEnabled(max_px > 0)

    def _on_timeline_scroll_value(self, v: int) -> None:
        sb = self.timeline_scrollbar
        if sb.value() != v:
            sb.blockSignals(True)
            sb.setValue(v)
            sb.blockSignals(False)

    # --- zoom bar ------------------------------------------------------

    def _pps_to_slider(self, pps: float) -> int:
        """Map pixels-per-second (log-scale between PPS_MIN and PPS_MAX) to
        the slider's 0..100 integer range."""
        import math
        lo = self.timeline.PPS_MIN
        hi = self.timeline.PPS_MAX
        pps = max(lo, min(hi, pps))
        return int(round(100.0 * math.log(pps / lo) / math.log(hi / lo)))

    def _slider_to_pps(self, val: int) -> float:
        import math
        lo = self.timeline.PPS_MIN
        hi = self.timeline.PPS_MAX
        return lo * (hi / lo) ** (max(0, min(100, val)) / 100.0)

    def _on_zoom_slider_changed(self, val: int) -> None:
        new_pps = self._slider_to_pps(val)
        if abs(new_pps - self.timeline.pixels_per_second()) > 0.01:
            self.timeline.set_pixels_per_second(new_pps)

    def _sync_zoom_slider(self, pps: float) -> None:
        """Called when the timeline reports a zoom change from wheel or
        code. Updates the slider position without re-emitting valueChanged."""
        target = self._pps_to_slider(pps)
        if self.zoom_slider.value() != target:
            self.zoom_slider.blockSignals(True)
            self.zoom_slider.setValue(target)
            self.zoom_slider.blockSignals(False)

    def _zoom_in_clicked(self) -> None:
        self.timeline.set_pixels_per_second(self.timeline.pixels_per_second() * 1.25)

    def _zoom_out_clicked(self) -> None:
        self.timeline.set_pixels_per_second(self.timeline.pixels_per_second() / 1.25)

    # --- undo / redo --------------------------------------------------

    def _current_state_snap(self) -> dict:
        return {
            "clips": [c.clone() for c in self._clips],
            "selected_id": self.timeline.selected_id() if hasattr(self, "timeline") else "",
            "playhead": self.timeline.playhead() if hasattr(self, "timeline") else 0.0,
            "added_audios": [a.clone() for a in self._added_audios],
            "replace_audio": self.audio_replace_cb.isChecked() if hasattr(self, "audio_replace_cb") else False,
            "added_gain": self.audio_gain.value() if hasattr(self, "audio_gain") else 1.0,
            "orig_gain": self.orig_gain.value() if hasattr(self, "orig_gain") else 1.0,
        }

    def _snapshot(self) -> None:
        """Record the current state as an undo point. Any queued redo
        entries are discarded because we're branching off a new action."""
        self._undo_stack.append(self._current_state_snap())
        if len(self._undo_stack) > self._undo_limit:
            self._undo_stack.pop(0)
        self._redo_stack.clear()

    def _apply_state(self, snap: dict) -> None:
        self._clips = [c.clone() for c in snap["clips"]]
        self._restore_added_audios(snap.get("added_audios", []))
        for w, val in (
            (self.audio_replace_cb, snap["replace_audio"]),
        ):
            w.blockSignals(True); w.setChecked(bool(val)); w.blockSignals(False)
        for w, val in (
            (self.audio_gain, snap["added_gain"]),
            (self.orig_gain, snap["orig_gain"]),
        ):
            w.blockSignals(True); w.setValue(float(val)); w.blockSignals(False)

        self.timeline.set_clips(self._clips)
        self._refresh_added_audio_display()

        sid = snap["selected_id"] or (self._clips[0].id if self._clips else "")
        if sid:
            self.timeline.select_clip(sid)
        self.timeline.set_playhead(snap["playhead"], emit=False)

        preview = next((c for c in self._clips if c.id == sid), None)
        if preview is None and self._clips:
            preview = self._clips[0]
        if preview is not None:
            self._set_preview_clip(preview)
        else:
            self._preview_clip_id = ""
            self.player.setSource(QUrl())
        # `_set_preview_clip()` refreshes the output gains, but the clipless
        # branch above doesn't — an audio-only timeline would keep the
        # pre-undo per-item added-audio gain on its live QAudioOutputs.
        self._update_audio_volumes()

        self._sync_selected_clip_ui()
        self._update_range_label()
        self._update_controls_enabled()
        # Re-sync the main player so the preview shows the right frame at
        # the restored playhead, not a stale one from before the undo.
        self._drive_main_player_from_playhead()

    def _undo(self) -> None:
        if not self._undo_stack:
            self.status.showMessage("Nothing to undo.", 2000)
            return
        self._redo_stack.append(self._current_state_snap())
        snap = self._undo_stack.pop()
        self._apply_state(snap)
        self.status.showMessage("Undone.", 1500)

    def _redo(self) -> None:
        if not self._redo_stack:
            self.status.showMessage("Nothing to redo.", 2000)
            return
        self._undo_stack.append(self._current_state_snap())
        if len(self._undo_stack) > self._undo_limit:
            self._undo_stack.pop(0)
        snap = self._redo_stack.pop()
        self._apply_state(snap)
        self.status.showMessage("Redone.", 1500)

    # --- UI construction ----------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        central.setObjectName("CentralRoot")
        self.setCentralWidget(central)
        root_vert = QVBoxLayout(central)
        root_vert.setContentsMargins(0, 0, 0, 0)
        root_vert.setSpacing(0)

        # --- custom titlebar (frameless chrome)
        self.titlebar = TitleBar(self, title="Cove Video Editor", version=__version__)
        self.titlebar.minimizeRequested.connect(self.showMinimized)
        self.titlebar.maxRestoreRequested.connect(self._toggle_maximize)
        self.titlebar.closeRequested.connect(self.close)
        root_vert.addWidget(self.titlebar, 0)

        # --- main content container
        body = QWidget()
        body.setObjectName("CoveBody")
        root_vert.addWidget(body, 1)
        root = QVBoxLayout(body)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(10)

        main_split = QSplitter(Qt.Vertical)
        main_split.setChildrenCollapsible(False)
        main_split.setHandleWidth(6)
        root.addWidget(main_split, stretch=1)

        preview_panel = QWidget()
        preview_lay = QVBoxLayout(preview_panel)
        preview_lay.setContentsMargins(0, 0, 0, 0)
        preview_lay.setSpacing(10)

        # top splitter: bin | preview
        top_split = QSplitter(Qt.Horizontal)
        top_split.setChildrenCollapsible(False)
        top_split.setHandleWidth(6)

        self.clip_bin = ClipBin()
        self.clip_bin.assetActivated.connect(self._on_asset_activated)
        self.clip_bin.assetDeleteRequested.connect(self._on_asset_delete_requested)
        self.clip_bin.filesDropped.connect(self._on_bin_files_dropped)
        self.clip_bin.subActivated.connect(self._on_sub_activated)
        self.clip_bin.subDeleteRequested.connect(self._on_sub_delete_requested)
        self.clip_bin.subStyleRequested.connect(self._on_sub_style_requested)
        self.clip_bin.subSyncRequested.connect(self._on_sub_sync_requested)
        top_split.addWidget(self.clip_bin)

        preview_box = QFrame()
        preview_box.setObjectName("PreviewStage")
        preview_box.setFrameShape(QFrame.NoFrame)
        pv_lay = QVBoxLayout(preview_box)
        pv_lay.setContentsMargins(0, 0, 0, 0)
        pv_lay.setSpacing(0)

        self.video_container = QWidget()
        self.video_container.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.video_view = VideoView(self.video_container)
        self.video_view.clicked.connect(self._toggle_play)
        self.video_view.contextMenuRequestedAt.connect(self._show_preview_menu)
        self.crop_overlay = CropOverlay(self.video_container)
        self.crop_overlay.setVisible(False)
        self.video_container.installEventFilter(self)
        pv_lay.addWidget(self.video_container, stretch=1)

        top_split.addWidget(preview_box)
        top_split.setStretchFactor(0, 0)
        top_split.setStretchFactor(1, 1)
        top_split.setSizes([300, 1120])
        preview_lay.addWidget(top_split, stretch=1)

        # Added-audio mode controls — no UI; we default to "mix with equal
        # gains" and flip to replace mode via the audio-track context menu.
        self.audio_replace_cb = QCheckBox(); self.audio_replace_cb.setVisible(False)
        self.audio_gain = QDoubleSpinBox(); self.audio_gain.setRange(0.0, 3.0); self.audio_gain.setValue(1.0); self.audio_gain.setVisible(False)
        self.orig_gain = QDoubleSpinBox(); self.orig_gain.setRange(0.0, 3.0); self.orig_gain.setValue(1.0); self.orig_gain.setVisible(False)

        # --- player
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.audio.setVolume(0.7)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.video_view.video_output())
        self.player.mediaStatusChanged.connect(self._on_media_status)

        # Timer-driven playhead — makes playback work through gaps between
        # clips and over audio-only timelines. Wall-clock advances the
        # playhead every tick; the main player is a passive video renderer
        # whose position is slaved to the timeline.
        self._play_timer = QTimer(self)
        self._play_timer.setInterval(30)
        self._play_timer.timeout.connect(self._on_play_tick)
        self._play_last_wall: float = 0.0
        self._play_region: tuple[float, float] | None = None

        # Throttle scrub seeks so rapid dragging doesn't queue up decoder
        # work faster than it can keep up. `_flush_pending_seek` guarantees
        # the last position lands even after the user stops moving.
        self._seek_throttle_timer = QTimer(self)
        self._seek_throttle_timer.setSingleShot(True)
        self._seek_throttle_timer.timeout.connect(self._flush_pending_seek)
        self._pending_seek_ms: int | None = None
        self._last_seek_wall: float = 0.0

        # One QMediaPlayer per added-audio entry (populated lazily in
        # `_append_added_audio`). Each plays once for its natural duration
        # inside its placement range and then pauses (no looping).

        # Dedicated player for the CURRENT clip's audio when it's been unlinked
        # from its video. We mute the main player's embedded audio for the
        # unlinked clip and push its audio through this player, offset by
        # `audio_offset`, so the waveform on the timeline matches what the
        # user hears.
        self.clip_audio_player = QMediaPlayer(self)
        self.clip_audio_output = QAudioOutput(self)
        self.clip_audio_output.setVolume(0.0)
        self.clip_audio_player.setAudioOutput(self.clip_audio_output)

        # --- transport row
        transport_bar = QFrame()
        transport_bar.setObjectName("TransportBar")
        transport = QHBoxLayout(transport_bar)
        transport.setContentsMargins(10, 8, 10, 8)
        transport.setSpacing(8)

        # Transport cluster: Rewind / Play-Pause / End. Grouped inside a
        # bordered box to match the design's "pill" treatment.
        transport_cluster = QFrame()
        transport_cluster.setObjectName("TransportCluster")
        transport_cluster.setStyleSheet(
            "QFrame#TransportCluster { background:#0a1013;"
            f" border:1px solid {theme.BORDER}; border-radius:8px; }}"
        )
        cluster_lay = QHBoxLayout(transport_cluster)
        cluster_lay.setContentsMargins(3, 3, 3, 3)
        cluster_lay.setSpacing(2)

        self.rewind_btn = _make_transport_btn("rewind", "Go to start")
        self.rewind_btn.clicked.connect(lambda: self.timeline.set_playhead(0.0))
        cluster_lay.addWidget(self.rewind_btn)

        self.play_btn = _make_transport_btn("play", "Play / Pause (Space)")
        self.play_btn.clicked.connect(self._toggle_play)
        cluster_lay.addWidget(self.play_btn)

        self.end_btn = _make_transport_btn("end", "Go to end")
        self.end_btn.clicked.connect(
            lambda: self.timeline.set_playhead(self._total_playback_length())
        )
        cluster_lay.addWidget(self.end_btn)
        transport.addWidget(transport_cluster)

        # Timecode box — read + write. Copy the exact time out when hand-
        # writing SRT cues, or type a time like `1:23.456` to jump there.
        self.timecode_edit = QLineEdit("0:00:00.000")
        self.timecode_edit.setObjectName("Timecode")
        self.timecode_edit.setFixedWidth(116)
        self.timecode_edit.setAlignment(Qt.AlignCenter)
        self.timecode_edit.setToolTip(
            "Current playhead. Accepts `SS.mmm`, `MM:SS.mmm`, or `H:MM:SS.mmm`."
        )
        self.timecode_edit.editingFinished.connect(self._on_timecode_edited)
        transport.addWidget(self.timecode_edit)

        # Thin divider
        divider = QFrame()
        divider.setFixedWidth(1)
        divider.setFixedHeight(22)
        divider.setStyleSheet(f"background:{theme.BORDER};")
        transport.addWidget(divider)

        self.split_btn = QPushButton("Split")
        self.split_btn.setToolTip("Split the clip under the playhead (S)")
        self.split_btn.clicked.connect(self._split_at_playhead)
        self.merge_btn = QPushButton("Merge")
        self.merge_btn.setToolTip(
            "Merge the selected clip with an adjacent same-source clip. "
            "If both sides are mergeable, pick a direction from the popup. "
            "Shortcuts: M (merge next), Shift+M (merge previous)."
        )
        self.merge_btn.clicked.connect(self._on_merge_button_clicked)
        self.delete_clip_btn = QPushButton("Delete clip")
        self.delete_clip_btn.clicked.connect(self._delete_selected_clip)
        self.download_video_btn = QPushButton("Download Video")
        self.download_video_btn.setToolTip("Download a video from a URL and add it at the playhead")
        self.download_video_btn.clicked.connect(self._on_download_video_clicked)
        self.crop_btn = QPushButton("Crop")
        self.crop_btn.setCheckable(True)
        self.crop_btn.toggled.connect(self._on_crop_toggled)
        self.crop_reset_btn = QPushButton("Reset crop")
        self.crop_reset_btn.setVisible(False)
        self.crop_reset_btn.clicked.connect(self._on_crop_reset)
        self.range_label = QLabel("—")
        self.range_label.setObjectName("RangeLabel")

        transport.addWidget(self.split_btn)
        transport.addWidget(self.merge_btn)
        transport.addWidget(self.delete_clip_btn)
        transport.addWidget(self.download_video_btn)
        transport.addSpacing(4)
        transport.addWidget(self.crop_btn)
        transport.addWidget(self.crop_reset_btn)
        transport.addStretch(1)
        transport.addWidget(self.range_label)
        preview_lay.addWidget(transport_bar)
        main_split.addWidget(preview_panel)

        timeline_panel = QWidget()
        timeline_lay = QVBoxLayout(timeline_panel)
        timeline_lay.setContentsMargins(0, 0, 0, 0)
        timeline_lay.setSpacing(10)

        # --- timeline
        self.timeline = TimelineWidget()
        self.timeline.playheadMoved.connect(self._on_timeline_playhead)
        self.timeline.clipSelected.connect(self._on_clip_selected)
        self.timeline.rangeChanged.connect(self._on_clip_range_changed)
        self.timeline.clipMoved.connect(self._on_clip_moved)
        self.timeline.selectionChanged.connect(self._on_selection_changed)
        self.timeline.regionDeleteRequested.connect(self._on_region_delete)
        self.timeline.regionCropRequested.connect(self._on_region_crop)
        self.timeline.regionExportRequested.connect(self._on_region_export)
        self.timeline.splitAtPlayheadRequested.connect(self._split_at_playhead)
        self.timeline.addedAudioDropped.connect(self._on_added_audio_dropped)
        self.timeline.videoFileDropped.connect(self._on_video_file_dropped)
        self.timeline.assetDroppedOnTimeline.connect(self._on_asset_dropped_on_timeline)
        self.timeline.addedAudioDeleteRequested.connect(self._on_added_audio_delete_requested)
        self.timeline.addedAudioReplaceToggled.connect(self._on_added_audio_replace_toggled)
        self.timeline.addedAudioOffsetChanged.connect(self._on_added_audio_offset_changed)
        self.timeline.addedAudioRangeChanged.connect(self._on_added_audio_range_changed)
        self.timeline.addedAudioVolumeRequested.connect(self._on_added_audio_volume_requested)
        self.timeline.clipDoubleClicked.connect(self._open_clip_properties)
        self.timeline.audioLinkToggled.connect(self._on_audio_link_toggled)
        self.timeline.clipDeleteRequested.connect(self._on_clip_delete_requested)
        self.timeline.clipMoveToStartRequested.connect(self._on_clip_move_to_start)
        self.timeline.clipMoveToPlayheadRequested.connect(self._on_clip_move_to_playhead)
        self.timeline.audioOffsetChanged.connect(self._on_audio_offset_changed)
        self.timeline.clipAudioRemoveRequested.connect(self._on_clip_audio_remove_requested)
        self.timeline.scrollRangeChanged.connect(self._on_timeline_scroll_range)
        self.timeline.scrollValueChanged.connect(self._on_timeline_scroll_value)
        timeline_lay.addWidget(self.timeline, stretch=1)

        # Scrollbar + VideoPad-style zoom bar share one row. Scrollbar
        # stretches; the zoom cluster sits on the right with a fixed width.
        sb_bar = QFrame()
        sb_bar.setObjectName("ZoomBar")
        sb_row = QHBoxLayout(sb_bar)
        sb_row.setContentsMargins(12, 7, 12, 7)
        sb_row.setSpacing(8)
        self.timeline_scrollbar = QScrollBar(Qt.Horizontal)
        self.timeline_scrollbar.setRange(0, 0)
        self.timeline_scrollbar.setMinimumWidth(120)
        self.timeline_scrollbar.valueChanged.connect(self.timeline.set_scroll_x)
        sb_row.addWidget(self.timeline_scrollbar, stretch=1)

        self.zoom_out_btn = _make_zoom_btn("minus", "Zoom out (Shift+Scroll also scrolls)")
        self.zoom_out_btn.clicked.connect(self._zoom_out_clicked)
        sb_row.addWidget(self.zoom_out_btn)

        self.zoom_slider = QSlider(Qt.Horizontal)
        self.zoom_slider.setRange(0, 100)
        self.zoom_slider.setFixedWidth(160)
        self.zoom_slider.setToolTip(
            "Zoom the timeline. Mouse wheel over the timeline does the same."
        )
        self.zoom_slider.valueChanged.connect(self._on_zoom_slider_changed)
        sb_row.addWidget(self.zoom_slider)

        self.zoom_in_btn = _make_zoom_btn("plus", "Zoom in")
        self.zoom_in_btn.clicked.connect(self._zoom_in_clicked)
        sb_row.addWidget(self.zoom_in_btn)

        timeline_lay.addWidget(sb_bar)

        # Sync slider when the user wheel-zooms on the timeline.
        self.timeline.pixelsPerSecondChanged.connect(self._sync_zoom_slider)
        self._sync_zoom_slider(self.timeline.pixels_per_second())

        # --- export row
        export_bar = QFrame()
        export_bar.setObjectName("ExportBar")
        bottom = QHBoxLayout(export_bar)
        bottom.setContentsMargins(14, 10, 14, 10)
        bottom.setSpacing(12)
        self.export_type_combo = QComboBox()
        self.export_type_combo.addItem("Project")
        self.export_type_combo.addItem("Audio Only")
        self.export_type_combo.setCurrentText("Project")
        self.export_type_combo.currentTextChanged.connect(self._on_export_type_changed)

        self.format_combo = QComboBox()
        self.format_combo.setMinimumWidth(220)
        self._populate_format_combo(False)

        # Resolution presets. Dimensions live in the item data (never parsed
        # back out of the label); "Auto" carries None so the exporter keeps
        # its first-real-clip behavior.
        self.resolution_combo = QComboBox()
        self.resolution_combo.addItem("Auto (First Clip)", None)
        for _label, _size in (
            ("3840 × 2160", (3840, 2160)),
            ("1920 × 1080", (1920, 1080)),
            ("1280 × 720", (1280, 720)),
            ("1080 × 1920", (1080, 1920)),
            ("1080 × 1080", (1080, 1080)),
        ):
            self.resolution_combo.addItem(_label, _size)
        self.resolution_combo.setCurrentIndex(0)

        export_lbl = QLabel("EXPORT")
        export_lbl.setObjectName("ExportLabel")
        as_lbl = QLabel("AS")
        as_lbl.setObjectName("ExportLabel")
        bottom.addWidget(export_lbl)
        bottom.addWidget(self.export_type_combo, stretch=0)
        bottom.addWidget(as_lbl)
        bottom.addWidget(self.format_combo, stretch=0)
        res_lbl = QLabel("Resolution")
        res_lbl.setObjectName("ExportLabel")
        bottom.addWidget(res_lbl)
        bottom.addWidget(self.resolution_combo, stretch=0)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        self.progress.setFormat("%p%")
        self._last_progress = 0
        self._last_eta: float | None = None
        bottom.addWidget(self.progress, stretch=1)

        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._on_cancel_clicked)
        bottom.addWidget(self.cancel_btn)

        self.details_btn = QPushButton("Details ▸")
        self.details_btn.setObjectName("FlatButton")
        self.details_btn.setCheckable(True)
        self.details_btn.setChecked(False)
        self.details_btn.setFixedHeight(28)
        self.details_btn.clicked.connect(self._on_details_toggled)
        bottom.addWidget(self.details_btn)

        self.export_btn = QPushButton("Export")
        self.export_btn.setObjectName("PrimaryButton")
        self.export_btn.setMinimumHeight(34)
        self.export_btn.clicked.connect(self._on_export_clicked)
        bottom.addWidget(self.export_btn)

        timeline_lay.addWidget(export_bar)

        # Collapsible export log — hidden by default, toggled by details_btn.
        self._log_panel = QFrame()
        self._log_panel.setObjectName("ExportLogPanel")
        log_lay = QVBoxLayout(self._log_panel)
        log_lay.setContentsMargins(14, 6, 14, 10)
        log_lay.setSpacing(6)

        self.export_log = QTextEdit()
        self.export_log.setReadOnly(True)
        self.export_log.setObjectName("ExportLog")
        self.export_log.setFixedHeight(130)
        self.export_log.setLineWrapMode(QTextEdit.NoWrap)
        self.export_log.document().setMaximumBlockCount(500)
        log_lay.addWidget(self.export_log)

        copy_log_btn = QPushButton("Copy log")
        copy_log_btn.setObjectName("FlatButton")
        copy_log_btn.setFixedHeight(24)
        copy_log_btn.clicked.connect(self._on_copy_log)
        log_lay.addWidget(copy_log_btn, alignment=Qt.AlignRight)

        self._log_panel.setVisible(False)
        timeline_lay.addWidget(self._log_panel)

        main_split.addWidget(timeline_panel)
        main_split.setStretchFactor(0, 3)
        main_split.setStretchFactor(1, 1)
        main_split.setSizes([520, 260])

        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.status.showMessage("Drop videos, audio, images, or subtitles into the Media panel.", 10000)

    # --- drag & drop into the whole window ---------------------------

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls() or event.mimeData().hasFormat(ASSET_MIME):
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:  # noqa: ANN001
        if event.mimeData().hasUrls() or event.mimeData().hasFormat(ASSET_MIME):
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        md = event.mimeData()
        if md.hasFormat(ASSET_MIME):
            asset_id = bytes(md.data(ASSET_MIME)).decode()
            asset = self._assets.get(asset_id)
            if asset and asset.kind == "audio":
                self._set_added_audio(asset.path)
            elif asset:
                # Video + image both land on the video track.
                self._append_clip_for_asset(asset_id)
            event.acceptProposedAction()
            return
        if md.hasUrls():
            paths = [Path(u.toLocalFile()) for u in md.urls() if u.toLocalFile()]
            audio_paths = [p for p in paths if p.suffix.lower() in AUDIO_EXTS]
            other_paths = [p for p in paths if p.suffix.lower() not in AUDIO_EXTS]
            if other_paths:
                # _import_paths handles video, image, and subtitle files.
                self._import_paths(other_paths)
            for p in audio_paths:
                self._append_added_audio(p)
            event.acceptProposedAction()

    def eventFilter(self, obj, event) -> bool:  # noqa: ANN001
        et = event.type()
        if getattr(self, "video_container", None) is obj and et == QEvent.Resize:
            r = self.video_container.rect()
            self.video_view.setGeometry(r)
            self.crop_overlay.setGeometry(r)
            self.crop_overlay.raise_()
        return super().eventFilter(obj, event)

    # --- video download ------------------------------------------------

    def _on_download_video_clicked(self) -> None:
        existing = self._download_dialog
        if existing is not None and existing.isVisible():
            existing.raise_()
            existing.activateWindow()
            return

        ffmpeg_path: str | None = None
        try:
            ffmpeg_path = ff.require_ffmpeg()
        except ff.FFmpegMissingError:
            # The startup dependency check already reports this; let yt-dlp
            # still try sites/formats that do not need a merge step.
            ffmpeg_path = None

        dlg = DownloadVideoDialog(self, ffmpeg_path=ffmpeg_path)
        dlg.downloadFinished.connect(self._on_download_video_finished)

        def _clear_dialog(_result: int) -> None:
            if self._download_dialog is dlg:
                self._download_dialog = None

        dlg.finished.connect(_clear_dialog)
        self._download_dialog = dlg
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _on_download_video_finished(self, path: Path) -> None:
        insert_t = self.timeline.playhead()
        existing = next((a for a in self._assets.values() if a.path == path), None)
        if existing is None:
            self._import_paths([path], append_to_timeline=False)
            existing = next((a for a in self._assets.values() if a.path == path), None)
        if existing is None or existing.kind != "video":
            QMessageBox.warning(
                self,
                "Import failed",
                f"Downloaded file could not be imported: {path.name}",
            )
            return
        self._insert_clip_at(existing.id, insert_t)
        self.status.showMessage(f"Downloaded and added {path.name}.", 6000)

    # --- importing ----------------------------------------------------

    def _import_paths(self, paths: list[Path], append_to_timeline: bool = True) -> None:
        new_assets: list[MediaAsset] = []
        for p in paths:
            if not p.exists():
                continue
            ext = p.suffix.lower()
            if ext in SUB_EXTS:
                self._import_sub(p)
                continue
            if ext in AUDIO_EXTS:
                try:
                    dur = ff.probe_audio_duration(p)
                except Exception as exc:  # noqa: BLE001
                    QMessageBox.warning(self, f"Could not open {p.name}", str(exc))
                    continue
                asset = MediaAsset(
                    path=p, duration=dur, width=0, height=0, fps=0.0,
                    has_audio=True, kind="audio",
                )
            elif ext in IMAGE_EXTS:
                asset = self._build_image_asset(p)
                if asset is None:
                    continue
            else:
                try:
                    info = ff.probe(p)
                except ff.FFmpegMissingError as exc:
                    QMessageBox.critical(self, "Missing dependency", str(exc))
                    return
                except Exception as exc:  # noqa: BLE001
                    QMessageBox.warning(self, f"Could not open {p.name}", str(exc))
                    continue
                asset = MediaAsset(
                    path=p, duration=info.duration,
                    width=info.width, height=info.height, fps=info.fps,
                    has_audio=info.has_audio, kind="video",
                )
            self._assets[asset.id] = asset
            self.clip_bin.add_asset(asset)
            new_assets.append(asset)
            if append_to_timeline and asset.kind == "video":
                self._append_clip_for_asset(asset.id)

        self._update_controls_enabled()

    def _build_image_asset(self, p: Path) -> MediaAsset | None:
        """Load `p` as a QImage and wrap it as an image MediaAsset. Returns
        None on failure (unsupported format or decode error).

        The asset's ``duration`` is the permissive upper cap so the
        properties dialog allows stretching the still card. Each Clip built
        from it defaults to DEFAULT_IMAGE_DURATION seconds on the timeline.
        """
        img = QImage(str(p))
        if img.isNull():
            QMessageBox.warning(
                self, f"Could not open {p.name}",
                "This image format isn't readable.",
            )
            return None
        asset = MediaAsset(
            path=p, duration=IMAGE_ASSET_DURATION_CAP,
            width=img.width(), height=img.height(),
            fps=0.0, has_audio=False, kind="image", thumb=img,
        )
        self._image_pixmaps[asset.id] = QPixmap.fromImage(img)
        return asset

    def _import_sub(self, p: Path) -> None:
        """Stash an SRT / VTT in the Subs list. The first imported subtitle
        is auto-activated so the user doesn't have to click twice to try
        burn-in on export."""
        existing = next((s for s in self._subs if s.path == p), None)
        if existing is not None:
            self._activate_sub(existing.id)
            self.status.showMessage(f"Subtitle already imported: {p.name}", 3000)
            return
        cues = parse_sub_cues(p)
        sub = SubtitleTrack(path=p, active=not self._subs, cues=cues)
        self._subs.append(sub)
        self.clip_bin.add_sub(sub.id, p.name, str(p), active=sub.active)
        if sub.active:
            self.clip_bin.set_active_sub(sub.id)
            self._refresh_subtitle_overlay()
        note = f" ({len(cues)} cues)" if cues else " (no cues parsed — live preview unavailable)"
        self.status.showMessage(f"Subtitle imported: {p.name}{note}", 5000)

    def _on_asset_activated(self, asset_id: str) -> None:
        asset = self._assets.get(asset_id)
        if asset is None:
            return
        if asset.kind == "audio":
            self._append_added_audio(asset.path)
        else:
            # Video and image assets both land on the video track.
            self._append_clip_for_asset(asset_id)

    def _activate_sub(self, sub_id: str) -> None:
        """Mark `sub_id` as the active (burn-in) subtitle; toggle the
        existing active one off if the user clicks it a second time."""
        target = next((s for s in self._subs if s.id == sub_id), None)
        if target is None:
            return
        already = target.active
        for s in self._subs:
            s.active = False
        if not already:
            target.active = True
        self.clip_bin.set_active_sub(target.id if target.active else "")
        self._refresh_subtitle_overlay()
        if target.active:
            self.status.showMessage(
                f"Burn-in on export: {target.path.name}", 4000,
            )
        else:
            self.status.showMessage("Subtitle burn-in disabled.", 3000)

    def _on_sub_activated(self, sub_id: str) -> None:
        self._activate_sub(sub_id)

    def _on_sub_delete_requested(self, sub_id: str) -> None:
        sub = next((s for s in self._subs if s.id == sub_id), None)
        if sub is None:
            return
        self._subs = [s for s in self._subs if s.id != sub_id]
        self.clip_bin.remove_sub(sub_id)
        # If we killed the active one, pick a new active (first remaining).
        if sub.active and self._subs:
            self._subs[0].active = True
            self.clip_bin.set_active_sub(self._subs[0].id)
        self._refresh_subtitle_overlay()
        self.status.showMessage(f"Subtitle removed: {sub.path.name}", 3000)

    def _refresh_subtitle_overlay(self) -> None:
        """Recompute and paint the live subtitle overlay from the active
        track's cues at the current playhead. Safe to call any time —
        no-op when no active sub exists."""
        active = next((s for s in self._subs if s.active), None)
        if active is None or not active.cues:
            self.video_view.hide_subtitle()
            return
        text = active.cue_at(self.timeline.playhead())
        if not text:
            self.video_view.hide_subtitle()
            return
        style = {
            "font_family": active.font_family,
            "font_size": active.font_size,
            "primary_color": active.primary_color,
            "outline_color": active.outline_color,
            "outline": active.outline,
            "position": active.position,
        }
        self.video_view.set_subtitle_cue(text, style)

    def _on_sub_style_requested(self) -> None:
        active = next((s for s in self._subs if s.active), None)
        if active is None:
            QMessageBox.information(
                self, "No active subtitle",
                "Import a subtitle file and double-click it to mark it active "
                "before editing its style.",
            )
            return
        # Snapshot the pre-dialog style so Cancel fully reverts the live
        # preview changes the user made while editing.
        saved = {
            "font_family": active.font_family,
            "font_size": active.font_size,
            "primary_color": active.primary_color,
            "outline_color": active.outline_color,
            "outline": active.outline,
            "position": active.position,
        }
        dlg = SubtitleStyleDialog(active, self)
        dlg.stylePreview.connect(self._apply_sub_style_preview)
        # Make sure a cue is visible while the dialog is open so the user
        # sees live changes. If no cue covers the playhead, jump to the
        # first cue so they have something to style against.
        if active.cues and not active.cue_at(self.timeline.playhead()):
            self.timeline.set_playhead(active.cues[0][0] + 0.05)
        result = dlg.exec()
        if result == QDialog.Accepted:
            vals = dlg.result_values() or saved
            active.font_family = vals.get("font_family", active.font_family)
            active.font_size = vals["font_size"]
            active.primary_color = vals["primary_color"]
            active.outline_color = vals["outline_color"]
            active.outline = vals["outline"]
            active.position = vals["position"]
            self.status.showMessage("Subtitle style updated.", 3000)
        else:
            # Revert — the live preview wrote changes into `active` as the
            # user typed; roll them back now.
            for k, v in saved.items():
                setattr(active, k, v)
        self._refresh_subtitle_overlay()

    def _apply_sub_style_preview(self, vals: dict) -> None:
        """Called by SubtitleStyleDialog on every widget change. Writes
        the in-progress style onto the active sub and repaints the
        overlay so the user sees the result in real time."""
        active = next((s for s in self._subs if s.active), None)
        if active is None:
            return
        active.font_family = vals.get("font_family", active.font_family)
        active.font_size = int(vals.get("font_size", active.font_size))
        active.primary_color = vals.get("primary_color", active.primary_color)
        active.outline_color = vals.get("outline_color", active.outline_color)
        active.outline = int(vals.get("outline", active.outline))
        active.position = vals.get("position", active.position)
        self._refresh_subtitle_overlay()

    # --- subtitle sync ------------------------------------------------

    def _on_sub_sync_requested(self) -> None:
        active = next((s for s in self._subs if s.active), None)
        if active is None:
            QMessageBox.information(
                self, "No active subtitle",
                "Import a subtitle file and mark it active before syncing.",
            )
            return
        if not active.cues:
            QMessageBox.information(
                self, "No cues parsed",
                ".ass / .ssa formats aren't parsed for in-app sync. "
                "Pre-sync them in a subtitle editor, then re-import.",
            )
            return
        # Modeless: a second click raises the existing dialog instead of
        # stacking a new one — lets the user keep scrubbing / playing the
        # video while dragging the offset slider and seeing the overlay
        # shift in real time.
        existing = getattr(self, "_sync_dialog", None)
        if existing is not None and existing.isVisible():
            existing.raise_()
            existing.activateWindow()
            return
        saved_offset = active.offset_ms
        dlg = SubtitleSyncDialog(active, self)
        dlg.offsetPreview.connect(self._apply_sub_offset_preview)

        def _on_closed(result: int) -> None:
            if result != QDialog.Accepted:
                current = next((s for s in self._subs if s.active), None)
                if current is not None:
                    current.offset_ms = saved_offset
                    self._refresh_subtitle_overlay()
            else:
                current = next((s for s in self._subs if s.active), None)
                if current is not None:
                    sign = "+" if current.offset_ms > 0 else ""
                    self.status.showMessage(
                        f"Subtitle sync offset: {sign}{current.offset_ms} ms",
                        4000,
                    )
            self._sync_dialog = None

        dlg.finished.connect(_on_closed)
        self._sync_dialog = dlg
        dlg.setModal(False)
        # Qt.Tool keeps the dialog floating over the main window without
        # pulling it out of the normal window stack order.
        dlg.setWindowFlag(Qt.Tool, True)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _apply_sub_offset_preview(self, offset_ms: int) -> None:
        active = next((s for s in self._subs if s.active), None)
        if active is None:
            return
        active.offset_ms = int(offset_ms)
        self._refresh_subtitle_overlay()

    def _on_bin_files_dropped(self, paths: list) -> None:
        # Import into the library only — don't auto-append videos to the
        # timeline. User drags to timeline explicitly when they want them.
        self._import_paths([Path(p) for p in paths if p], append_to_timeline=False)

    def _on_asset_delete_requested(self, asset_id: str) -> None:
        asset = self._assets.get(asset_id)
        if asset is None:
            return
        affected = [c for c in self._clips if c.asset.id == asset_id]
        # Snapshot before deleting so Ctrl+Z can bring it back (state only;
        # the bin tiles themselves aren't in the snapshot).
        self._snapshot()
        # If any timeline clips use this asset, drop them.
        if affected:
            self._clips = [c for c in self._clips if c.asset.id != asset_id]
            self.timeline.set_clips(self._clips)
            if self._preview_clip_id in {c.id for c in affected}:
                self._preview_clip_id = ""
                self.player.setSource(QUrl())
        # Drop any added-audio entries that referenced this asset's path.
        doomed = [a.id for a in self._added_audios if a.path == asset.path]
        if doomed:
            for aid in doomed:
                self._destroy_added_player(aid)
            self._added_audios = [
                a for a in self._added_audios if a.id not in doomed
            ]
            self._refresh_added_audio_display()
            self._update_audio_volumes()
        self._assets.pop(asset_id, None)
        self._image_pixmaps.pop(asset_id, None)
        self.clip_bin.remove_asset(asset_id)
        if not self._clips:
            self._halt_playback_no_clips()
        self._sync_selected_clip_ui()
        self._update_range_label()
        self._update_controls_enabled()
        self.status.showMessage(f"Removed {asset.path.name}.", 3000)

    def _append_clip_for_asset(self, asset_id: str) -> None:
        self._insert_clip_at(asset_id, sequence_length(self._clips))

    def _insert_clip_at(self, asset_id: str, drop_t: float) -> None:
        """Add a video or image clip to the timeline at `drop_t`. If the
        position falls inside an existing clip, advance to the end of that
        clip so the new one lands cleanly to its right."""
        asset = self._assets.get(asset_id)
        if asset is None or asset.kind not in ("video", "image"):
            return
        self._snapshot()
        start_t = max(0.0, drop_t)
        for c in sort_clips(self._clips):
            if c.timeline_start <= start_t < c.timeline_end:
                start_t = c.timeline_end
        if asset.kind == "image":
            # Image clips land at the compact default card length; the user
            # can stretch them in the properties dialog (bounded by the
            # asset's `duration` cap).
            clip = Clip(
                asset=asset, timeline_start=start_t,
                src_start=0.0, src_end=DEFAULT_IMAGE_DURATION,
            )
        else:
            clip = Clip(asset=asset, timeline_start=start_t)
        self._clips.append(clip)
        self._clips = sort_clips(self._clips)
        self.timeline.set_clips(self._clips)
        self.timeline.select_clip(clip.id)
        if asset.kind == "image":
            self._seed_image_clip_thumbs(clip)
        else:
            self._kick_off_thumbs(clip)
            if asset.has_audio:
                self._kick_off_waveform(clip)
        # For the very first clip, move the playhead onto it so the preview
        # actually shows the first frame instead of staying black at t=0 in
        # an area that may or may not contain the new clip.
        if len(self._clips) == 1:
            self.timeline.set_playhead(clip.timeline_start)
        if not self._preview_clip_id:
            self._set_preview_clip(clip)
        # Refresh the preview so the video item is visible + positioned on
        # the right frame as soon as the clip lands on the timeline.
        self._drive_main_player_from_playhead()
        self._sync_selected_clip_ui()
        self._update_range_label()
        self._update_controls_enabled()

    def _seed_image_clip_thumbs(self, clip: Clip) -> None:
        """Image clips never run a thumbnail worker — the asset is the only
        'frame'. Seed `thumb_pixmaps` with one scaled pixmap so the timeline
        strip renders it like a tiled backdrop."""
        pm = self._image_pixmaps.get(clip.asset.id)
        if pm is None:
            img = clip.asset.thumb
            if img is not None:
                pm = QPixmap.fromImage(img)
                self._image_pixmaps[clip.asset.id] = pm
        if pm is not None:
            clip.thumb_pixmaps = [pm]

    def _on_asset_dropped_on_timeline(
        self, asset_id: str, drop_t: float, lane: int = 1,
    ) -> None:
        asset = self._assets.get(asset_id)
        if asset is None:
            return
        if asset.kind == "audio":
            self._append_added_audio(
                asset.path, initial_offset=drop_t, lane=int(lane),
            )
        else:
            # Video + image both go to the single video track.
            self._insert_clip_at(asset_id, drop_t)

    def _on_video_file_dropped(self, path: str, drop_t: float) -> None:
        p = Path(path)
        if not p.exists():
            return
        # Reuse an already-imported asset for the same file.
        existing = next(
            (a for a in self._assets.values() if a.path == p), None,
        )
        if existing is not None:
            self._insert_clip_at(existing.id, drop_t)
            return
        try:
            info = ff.probe(p)
        except ff.FFmpegMissingError as exc:
            QMessageBox.critical(self, "Missing dependency", str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, f"Could not open {p.name}", str(exc))
            return
        asset = MediaAsset(
            path=p, duration=info.duration,
            width=info.width, height=info.height, fps=info.fps,
            has_audio=info.has_audio, kind="video",
        )
        self._assets[asset.id] = asset
        self.clip_bin.add_asset(asset)
        self._insert_clip_at(asset.id, drop_t)

    def _set_preview_clip(self, clip: Clip) -> None:
        self._preview_clip_id = clip.id
        self.video_view.set_native_size(clip.asset.width, clip.asset.height)
        self.crop_overlay.set_video_aspect(clip.asset.width / max(1, clip.asset.height))
        if clip.asset.kind == "image":
            # Image clips don't drive the media player — they're a static
            # pixmap overlay. Drop the player sources so no background
            # audio leaks through.
            self.player.pause()
            self.player.setSource(QUrl())
            self.clip_audio_player.pause()
            self.clip_audio_player.setSource(QUrl())
            pm = self._image_pixmaps.get(clip.asset.id)
            if pm is None and clip.asset.thumb is not None:
                pm = QPixmap.fromImage(clip.asset.thumb)
                self._image_pixmaps[clip.asset.id] = pm
            if pm is not None:
                self.video_view.show_image(pm, clip.asset.width, clip.asset.height)
            self._update_audio_volumes()
            return
        # Video path (unchanged).
        self.video_view.hide_image()
        # Pause before swapping sources — some Qt backends inherit the
        # previous media's playback state when a new source is loaded.
        self.player.pause()
        self.player.setSource(QUrl.fromLocalFile(str(clip.path)))
        # Seek to the frame under the playhead for this clip, not the clip's
        # trim start, so the preview reflects "current selection".
        t = self.timeline.playhead()
        src_t = clip.src_for_timeline(t)
        self.player.setPosition(int(max(0.0, src_t) * 1000))
        # And pause again in case the backend kicked into PlayingState
        # during the load. Timer is the only thing allowed to start playback.
        if not self._play_timer.isActive():
            self.player.pause()
        # Swap the unlinked-audio player's source so it's ready if this
        # clip is unlinked (otherwise the first tick picks it up late).
        self.clip_audio_player.pause()
        self.clip_audio_player.setSource(QUrl.fromLocalFile(str(clip.path)))
        self._update_audio_volumes()

    def _kick_off_thumbs(self, clip: Clip) -> None:
        # More thumbs for longer clips so the strip actually shows the scene
        # changing. Capped so import time stays tolerable.
        dur = max(1.0, clip.asset.duration)
        count = max(16, min(60, int(dur)))
        thread, worker = start_thumbnails(clip.id, clip.path, clip.asset.duration, count=count)
        worker.finished.connect(self._on_thumbs_ready, Qt.QueuedConnection)
        worker.failed.connect(self._on_thumb_error, Qt.QueuedConnection)
        thread.finished.connect(
            lambda cid=clip.id: self._thumb_done(cid), Qt.QueuedConnection,
        )
        self._thumb_threads[clip.id] = thread
        self._thumb_workers[clip.id] = worker
        thread.start()

    def _kick_off_waveform(self, clip: Clip) -> None:
        thread, worker = start_waveform(clip.id, clip.path)
        worker.finished.connect(self._on_waveform_ready, Qt.QueuedConnection)
        worker.failed.connect(self._on_waveform_error, Qt.QueuedConnection)
        thread.finished.connect(
            lambda cid=clip.id: self._wave_done(cid), Qt.QueuedConnection,
        )
        self._wave_threads[clip.id] = thread
        self._wave_workers[clip.id] = worker
        thread.start()

    def _thumb_done(self, clip_id: str) -> None:
        self._thumb_threads.pop(clip_id, None)
        self._thumb_workers.pop(clip_id, None)

    def _wave_done(self, clip_id: str) -> None:
        self._wave_threads.pop(clip_id, None)
        self._wave_workers.pop(clip_id, None)

    def _on_thumbs_ready(self, clip_id: str, images: list) -> None:
        clip = next((c for c in self._clips if c.id == clip_id), None)
        if not clip:
            return
        clip.thumbs = images
        clip.thumb_pixmaps = [QPixmap.fromImage(img) for img in images]
        if images and clip.asset.thumb is None:
            clip.asset.thumb = images[len(images) // 2]
            self.clip_bin.set_asset_thumb(clip.asset.id, clip.asset.thumb)
        self.timeline.update()

    def _on_thumb_error(self, _clip_id: str, msg: str) -> None:
        self.status.showMessage(f"Thumbnail error: {msg}", 4000)

    def _on_waveform_ready(self, clip_id: str, peaks: list, rate: int) -> None:
        clip = next((c for c in self._clips if c.id == clip_id), None)
        if clip and peaks:
            clip.waveform_peaks = list(peaks)
            clip.waveform_rate = int(rate)
            self.timeline.update()

    def _on_waveform_error(self, _clip_id: str, _msg: str) -> None:
        pass

    # --- navigation helpers ------------------------------------------

    def _current_fps(self) -> float:
        c = self._current_preview_clip()
        if c is None:
            c = self._clips[0] if self._clips else None
        if c is not None and c.asset.fps > 0:
            return float(c.asset.fps)
        return 30.0

    def _step_playhead(self, delta_s: float) -> None:
        if self._play_timer.isActive():
            # Pause so the stepped frame actually stays visible.
            self._toggle_play()
        t = self.timeline.playhead() + delta_s
        t = max(0.0, min(self._total_playback_length(), t))
        self.timeline.set_playhead(t)

    def _next_frame(self) -> None:
        self._step_playhead(1.0 / self._current_fps())

    def _prev_frame(self) -> None:
        self._step_playhead(-1.0 / self._current_fps())

    def _jump_selected_clip_start(self) -> None:
        c = self._selected_clip() or clip_at_timeline(self._clips, self.timeline.playhead())
        if c is not None:
            self.timeline.set_playhead(c.timeline_start)
        else:
            self.timeline.set_playhead(0.0)

    def _jump_selected_clip_end(self) -> None:
        c = self._selected_clip() or clip_at_timeline(self._clips, self.timeline.playhead())
        if c is not None:
            self.timeline.set_playhead(max(0.0, c.timeline_end - 1e-3))
        else:
            self.timeline.set_playhead(self._total_playback_length())

    def _all_clip_edges(self) -> list[float]:
        edges = [0.0, self._total_playback_length()]
        for c in self._clips:
            edges.append(c.timeline_start)
            edges.append(c.timeline_end)
        return sorted(set(edges))

    def _jump_prev_clip_edge(self) -> None:
        t = self.timeline.playhead()
        prev = [e for e in self._all_clip_edges() if e < t - 1e-3]
        if prev:
            self.timeline.set_playhead(prev[-1])

    def _jump_next_clip_edge(self) -> None:
        t = self.timeline.playhead()
        nxt = [e for e in self._all_clip_edges() if e > t + 1e-3]
        if nxt:
            self.timeline.set_playhead(nxt[0])

    def _on_escape_pressed(self) -> None:
        if self.crop_btn.isChecked():
            self.crop_btn.setChecked(False)

    # --- selection / editing -----------------------------------------

    def _selected_clip(self) -> Clip | None:
        sid = self.timeline.selected_id()
        return next((c for c in self._clips if c.id == sid), None)

    def _sync_selected_clip_ui(self) -> None:
        # The settings panel is gone; the transport just shows sequence info.
        pass

    def _on_clip_selected(self, clip_id: str) -> None:
        c = next((c for c in self._clips if c.id == clip_id), None)
        if c and self._preview_clip_id != c.id:
            self._set_preview_clip(c)
        self._sync_selected_clip_ui()
        self._update_audio_volumes()
        self._update_controls_enabled()

    def _on_clip_range_changed(self, _id: str, _s: float, _e: float) -> None:
        self._snapshot()
        self._clips = sort_clips(self._clips)
        self.timeline.set_clips(self._clips)
        self._sync_selected_clip_ui()
        self._update_range_label()

    def _on_clip_moved(self, _id: str, _start: float) -> None:
        self._snapshot()
        self._clips = sort_clips(self._clips)
        self.timeline.set_clips(self._clips)
        self._update_range_label()

    def _open_clip_properties(self, clip_id: str) -> None:
        c = next((c for c in self._clips if c.id == clip_id), None)
        if c is None:
            return
        dlg = ClipPropertiesDialog(c, self)
        if dlg.exec() == QDialog.Accepted:
            vals = dlg.result_values() or {}
            changed = (
                abs(c.speed - vals["speed"]) > 1e-6
                or abs(c.src_start - vals["src_start"]) > 1e-4
                or abs(c.src_end - vals["src_end"]) > 1e-4
                or c.muted != vals["muted"]
                or abs(c.audio_volume - vals["audio_volume"]) > 1e-6
            )
            if not changed:
                return
            self._snapshot()
            c.speed = vals["speed"]
            c.src_start = vals["src_start"]
            c.src_end = vals["src_end"]
            c.muted = vals["muted"]
            c.audio_volume = vals["audio_volume"]
            self.timeline.set_clips(self._clips)
            self._update_range_label()
            self._update_audio_volumes()

    def _split_at_playhead(self) -> None:
        t = self.timeline.playhead()
        # If an added-audio clip is selected, split that first.
        sel_audio_id = self.timeline._added_audio_selected_id
        if sel_audio_id:
            audio = next((a for a in self._added_audios if a.id == sel_audio_id), None)
            if audio is not None and audio.offset <= t < audio.timeline_end:
                self._split_added_audio_at(audio, t)
                return
        c = clip_at_timeline(self._clips, t)
        if c is not None:
            self._snapshot()
            new = split_clip(c, t)
            if new is None:
                self._undo_stack.pop()
                self.status.showMessage("Playhead is too close to a clip edge.", 3000)
                return
            self._clips.append(new)
            self._clips = sort_clips(self._clips)
            self.timeline.set_clips(self._clips)
            self.timeline.select_clip(new.id)
            self._sync_selected_clip_ui()
            self._drive_main_player_from_playhead()
            return
        # No video clip — try any added-audio under the playhead.
        audio = next(
            (a for a in self._added_audios if a.offset <= t < a.timeline_end),
            None,
        )
        if audio is not None:
            self._split_added_audio_at(audio, t)
            return
        self.status.showMessage("Playhead isn't over a clip.", 3000)

    def _split_added_audio_at(self, audio: AddedAudio, t: float) -> None:
        self._snapshot()
        new = split_added_audio(audio, t)
        if new is None:
            self._undo_stack.pop()
            self.status.showMessage("Playhead is too close to an audio clip edge.", 3000)
            return
        new.peaks = audio.peaks
        new.rate = audio.rate
        self._added_audios.append(new)
        self._create_added_player(new)
        self._refresh_added_audio_display()
        self._sync_added_audio_playback()
        self._update_controls_enabled()

    def _merge_added_audio(self, audio: AddedAudio) -> None:
        same_file = sorted(
            [a for a in self._added_audios
             if a.path == audio.path and a.lane == audio.lane],
            key=lambda a: a.offset,
        )
        idx = next((i for i, a in enumerate(same_file) if a.id == audio.id), -1)
        if idx < 0:
            return
        merged = False
        # Try merge with next neighbour first.
        if idx + 1 < len(same_file):
            nxt = same_file[idx + 1]
            if (abs(audio.src_end - nxt.src_start) < 0.05
                    and abs(audio.timeline_end - nxt.offset) < 0.05):
                self._snapshot()
                audio.src_end = nxt.src_end
                self._destroy_added_player(nxt.id)
                self._added_audios = [a for a in self._added_audios if a.id != nxt.id]
                merged = True
        # If not merged with next, try previous.
        if not merged and idx - 1 >= 0:
            prev = same_file[idx - 1]
            if (abs(prev.src_end - audio.src_start) < 0.05
                    and abs(prev.timeline_end - audio.offset) < 0.05):
                self._snapshot()
                prev.src_end = audio.src_end
                self._destroy_added_player(audio.id)
                self._added_audios = [a for a in self._added_audios if a.id != audio.id]
                merged = True
        if merged:
            self._refresh_added_audio_display()
            self._sync_added_audio_playback()
            self._update_controls_enabled()
            self.status.showMessage("Audio clips merged.", 2500)
        else:
            self.status.showMessage(
                "No adjacent audio clip from the same source to merge with.", 3500,
            )

    def _merge_with_next_clip(self) -> None:
        """Inverse of split — merge the selected clip with the next one
        when they came from the same asset with continuous source/timeline
        endpoints."""
        self._do_merge(direction=+1)

    def _merge_with_previous_clip(self) -> None:
        """Merge the selected clip with the previous one (Shift+M)."""
        self._do_merge(direction=-1)

    def _on_merge_button_clicked(self) -> None:
        """Merge-button entry point that lets the user pick a direction when
        both neighbours are mergeable instead of silently picking 'next'."""
        # Selected audio takes priority over video-at-playhead.
        sel_audio_id = self.timeline._added_audio_selected_id
        if sel_audio_id:
            audio = next((a for a in self._added_audios if a.id == sel_audio_id), None)
            if audio is not None:
                self._merge_added_audio(audio)
                return
        c = self._selected_clip() or (
            clip_at_timeline(self._clips, self.timeline.playhead())
            if self._clips else None
        )
        if c is None:
            self.status.showMessage("Select a clip to merge.", 3000)
            return
        dirs = self._available_merge_directions(c)
        if not dirs:
            self.status.showMessage(
                "No adjacent clip from the same source to merge with.", 3500,
            )
            return
        if len(dirs) == 1:
            self._do_merge(direction=+1 if "next" in dirs else -1)
            return
        menu = QMenu(self)
        menu.addAction("Merge with previous", self._merge_with_previous_clip)
        menu.addAction("Merge with next", self._merge_with_next_clip)
        pos = self.merge_btn.mapToGlobal(self.merge_btn.rect().bottomLeft())
        menu.exec(pos)

    def _available_merge_directions(self, c: Clip) -> set[str]:
        """Return which sides (`"prev"`, `"next"`) of `c` are mergeable —
        adjacent, same source, continuous src+timeline, same speed."""
        ordered = sort_clips(self._clips)
        idx = next((i for i, cc in enumerate(ordered) if cc.id == c.id), -1)
        if idx < 0:
            return set()
        def compat(left: Clip, right: Clip) -> bool:
            return (
                left.asset.id == right.asset.id
                and abs(left.src_end - right.src_start) < 0.05
                and abs(left.timeline_end - right.timeline_start) < 0.05
                and abs(left.speed - right.speed) < 1e-3
            )
        dirs: set[str] = set()
        if idx + 1 < len(ordered) and compat(c, ordered[idx + 1]):
            dirs.add("next")
        if idx - 1 >= 0 and compat(ordered[idx - 1], c):
            dirs.add("prev")
        return dirs

    def _do_merge(self, direction: int) -> None:
        c = self._selected_clip() or clip_at_timeline(self._clips, self.timeline.playhead())
        if c is None:
            self.status.showMessage("Select a clip to merge.", 3000)
            return
        ordered = sort_clips(self._clips)
        idx = next((i for i, cc in enumerate(ordered) if cc.id == c.id), -1)
        if idx < 0:
            return
        other_idx = idx + direction
        if other_idx < 0 or other_idx >= len(ordered):
            side = "right" if direction > 0 else "left"
            self.status.showMessage(f"No clip to merge with on the {side}.", 3000)
            return
        other = ordered[other_idx]
        left, right = (other, c) if direction < 0 else (c, other)
        if (
            left.asset.id != right.asset.id
            or abs(left.src_end - right.src_start) > 0.05
            or abs(left.timeline_end - right.timeline_start) > 0.05
            or abs(left.speed - right.speed) > 1e-3
        ):
            self.status.showMessage(
                "Can only merge adjacent clips from the same source.", 3500,
            )
            return
        self._snapshot()
        left.src_end = right.src_end
        self._clips = [cc for cc in self._clips if cc.id != right.id]
        self.timeline.set_clips(self._clips)
        self.timeline.select_clip(left.id)
        self._drive_main_player_from_playhead()
        self._update_range_label()
        self.status.showMessage("Clips merged.", 2500)

    def _delete_selected_clip(self) -> None:
        c = self._selected_clip()
        if c:
            self._delete_clip_by_id(c.id)
            return
        sel_audio_id = self.timeline._added_audio_selected_id
        if sel_audio_id:
            self.timeline._added_audio_selected_id = ""
            self._remove_added_audio(sel_audio_id)
            return

    def _on_clip_move_to_start(self, clip_id: str) -> None:
        self._ripple_move_clip(clip_id, 0.0)

    def _on_clip_move_to_playhead(self, clip_id: str) -> None:
        # The playhead is a target reference here, not something that moves.
        self._ripple_move_clip(clip_id, self.timeline.playhead())

    def _ripple_move_clip(self, clip_id: str, target_start: float) -> None:
        """Send a visual clip to `target_start` as one undoable action.

        This is the zero-drag way to close the leading gap left behind by
        trimming the head off the first clip, which otherwise strands the
        beginning of a long sequence off to the left. `plan_ripple_move`
        owns the position math; this only applies it and republishes.

        AddedAudio is untouched: it is an independent absolute-time layer
        with no stored link to any clip, so moving video must not guess at
        a sync relationship the project does not record.
        """
        plan = plan_ripple_move(self._clips, clip_id, target_start)
        if plan is None:
            return
        self._snapshot()
        for c in self._clips:
            new_start = plan[c.id]
            if not c.linked_audio:
                # Detached clip audio is pinned to absolute timeline time,
                # exactly as dragging a clip already treats it. Compensate
                # the offset so a ripple never slides it out of sync.
                c.audio_offset += c.timeline_start - new_start
            c.timeline_start = new_start
        self._clips = sort_clips(self._clips)
        self.timeline.set_clips(self._clips)
        self.timeline.select_clip(clip_id)
        self._sync_selected_clip_ui()
        self._update_range_label()
        self._drive_main_player_from_playhead()
        self.status.showMessage("Clip moved.", 2000)

    def _on_clip_delete_requested(self, clip_id: str) -> None:
        self._delete_clip_by_id(clip_id)

    def _delete_clip_by_id(self, clip_id: str) -> None:
        if not any(c.id == clip_id for c in self._clips):
            return
        self._snapshot()
        self._clips = [cc for cc in self._clips if cc.id != clip_id]
        self.timeline.set_clips(self._clips)
        self._sync_selected_clip_ui()
        self._update_range_label()
        if not self._clips:
            self._halt_playback_no_clips()
        elif self._preview_clip_id == clip_id:
            self._preview_clip_id = ""
            first = self._clips[0]
            self._set_preview_clip(first)
            self.timeline.select_clip(first.id)
        self.status.showMessage("Clip deleted.", 2500)

    def _halt_playback_no_clips(self) -> None:
        """Called when the timeline's clip list emptied out. Stops the
        video player and the unlinked-clip player; added audio keeps
        playing if there are still entries."""
        self.player.setSource(QUrl())
        self.clip_audio_player.pause()
        self.clip_audio_player.setSource(QUrl())
        self._preview_clip_id = ""
        if not self._added_audios:
            # Nothing at all to play — fully stop playback.
            if self._play_timer.isActive():
                self._play_timer.stop()
            self._pause_all_added_players()
            _swap_btn_icon(self.play_btn, "play")
        self._update_controls_enabled()

    def _on_audio_offset_changed(self, _clip_id: str, _offset: float) -> None:
        self._snapshot()
        self._sync_clip_audio_playback()

    def _on_clip_audio_remove_requested(self, clip_id: str) -> None:
        c = next((cc for cc in self._clips if cc.id == clip_id), None)
        if c is None or not c.asset.has_audio:
            return
        self._snapshot()
        c.audio_removed = True
        c.linked_audio = True
        c.audio_offset = 0.0
        self.timeline.set_clips(self._clips)
        self._update_audio_volumes()
        self._sync_clip_audio_playback()
        self.status.showMessage(
            "Audio removed — chain chip restores it.", 3500,
        )

    # --- timeline region actions -------------------------------------

    def _on_selection_changed(self, start: float, end: float) -> None:
        if end <= start:
            self.status.clearMessage()
        else:
            self.status.showMessage(f"Selected region: {_fmt(start)} → {_fmt(end)}  ({_fmt(end - start)})", 0)

    def _on_region_delete(self, start: float, end: float) -> None:
        self._snapshot()
        self._clips = delete_region(self._clips, start, end)
        self._delete_added_audio_region(start, end)
        self.timeline.set_clips(self._clips)
        self._refresh_added_audio_display()
        self.timeline.clear_selection()
        self._sync_selected_clip_ui()
        self._update_range_label()
        if not self._clips:
            self._halt_playback_no_clips()

    def _on_region_crop(self, start: float, end: float) -> None:
        self._snapshot()
        self._clips = keep_only_region(self._clips, start, end)
        self._crop_added_audio_to_region(start, end)
        self.timeline.set_clips(self._clips)
        self._refresh_added_audio_display()
        self.timeline.clear_selection()
        self._sync_selected_clip_ui()
        self._update_range_label()
        self.timeline.set_playhead(0.0)

    def _delete_added_audio_region(self, start: float, end: float) -> None:
        if end <= start:
            return
        dur = end - start
        kept: list[AddedAudio] = []
        for a in self._added_audios:
            ae = a.timeline_end
            if ae <= start:
                kept.append(a)
            elif a.offset >= end:
                a.offset -= dur
                kept.append(a)
            elif a.offset >= start and ae <= end:
                self._destroy_added_player(a.id)
            elif a.offset < start and ae > end:
                right = a.clone()
                right.id = __import__("uuid").uuid4().hex[:8]
                right.src_start = a.src_for_timeline(end)
                a.src_end = a.src_for_timeline(start)
                right.offset = start
                kept.append(a)
                kept.append(right)
                self._create_added_player(right)
            elif a.offset < start:
                a.src_end = a.src_for_timeline(start)
                kept.append(a)
            else:
                a.src_start = a.src_for_timeline(end)
                a.offset = start
                kept.append(a)
        self._added_audios = kept

    def _crop_added_audio_to_region(self, start: float, end: float) -> None:
        if end <= start:
            return
        kept: list[AddedAudio] = []
        for a in self._added_audios:
            ae = a.timeline_end
            if ae <= start or a.offset >= end:
                self._destroy_added_player(a.id)
                continue
            new_src_start = a.src_for_timeline(start) if a.offset < start else a.src_start
            new_src_end = a.src_for_timeline(end) if ae > end else a.src_end
            a.src_start = new_src_start
            a.src_end = new_src_end
            a.offset = max(0.0, a.offset - start)
            kept.append(a)
        self._added_audios = kept

    def _on_region_export(self, start: float, end: float) -> None:
        self._region_export_range = (start, end)
        try:
            self._on_export_clicked()
        finally:
            self._region_export_range = None

    # --- player -------------------------------------------------------

    def _toggle_play(self) -> None:
        if self._play_timer.isActive():
            self._play_timer.stop()
            self.player.pause()
            self._pause_all_added_players()
            self.clip_audio_player.pause()
            _swap_btn_icon(self.play_btn, "play")
            self._play_region = None
            return
        # Need at least one clip OR one added-audio entry to play.
        if not self._clips and not self._added_audios:
            return
        # If a region is selected, play only that region.
        sel_s = self.timeline._sel_start
        sel_e = self.timeline._sel_end
        if sel_e > sel_s:
            self._play_region = (sel_s, sel_e)
            if self.timeline.playhead() < sel_s or self.timeline.playhead() >= sel_e - 1e-3:
                self.timeline.set_playhead(sel_s, emit=False)
        else:
            self._play_region = None
            total = self._total_playback_length()
            if self.timeline.playhead() >= total - 1e-3:
                self.timeline.set_playhead(0.0, emit=False)
        self._update_audio_volumes()
        self._play_last_wall = time.monotonic()
        self._play_timer.start()
        self._drive_main_player_from_playhead()
        self._sync_clip_audio_playback()
        self._sync_added_audio_playback()
        _swap_btn_icon(self.play_btn, "pause")

    def _total_playback_length(self) -> float:
        clip_end = sequence_length(self._clips)
        audio_end = max(
            (a.timeline_end for a in self._added_audios), default=0.0,
        )
        return max(clip_end, audio_end)

    def _on_play_tick(self) -> None:
        now = time.monotonic()
        dt = now - self._play_last_wall
        self._play_last_wall = now
        new_t = self.timeline.playhead() + dt
        if self._play_region is not None:
            end = self._play_region[1]
            if new_t >= end:
                new_t = end
                self.timeline.set_playhead(new_t, emit=False)
                self._toggle_play()
                return
        else:
            total = self._total_playback_length()
            if total > 0 and new_t >= total:
                new_t = total
                self.timeline.set_playhead(new_t, emit=False)
                self._toggle_play()
                return
        self.timeline.set_playhead(new_t, emit=False)
        self._drive_main_player_from_playhead()
        self._sync_clip_audio_playback()
        self._sync_added_audio_playback()
        self._refresh_subtitle_overlay()
        self._update_timecode_display()

    def _drive_main_player_from_playhead(self) -> None:
        """Make the main player mirror where the playhead is on the timeline.
        When the play timer is idle this just seeks so the preview shows the
        right frame; it never auto-plays."""
        t = self.timeline.playhead()
        clip = clip_at_timeline(self._clips, t) if self._clips else None
        if clip is None:
            if self.player.playbackState() == QMediaPlayer.PlayingState:
                self.player.pause()
            self.video_view.set_video_visible(False)
            self.video_view.hide_image()
            return
        if clip.id != self._preview_clip_id:
            self._set_preview_clip(clip)
        if clip.asset.kind == "image":
            # Keep the image pinned on screen. The play timer continues to
            # advance the playhead so the image clip occupies its timeline
            # span just like a video clip would.
            if self.player.playbackState() == QMediaPlayer.PlayingState:
                self.player.pause()
            pm = self._image_pixmaps.get(clip.asset.id)
            if pm is not None:
                self.video_view.show_image(pm, clip.asset.width, clip.asset.height)
            return
        self.video_view.set_video_visible(True)
        src_t = clip.src_for_timeline(t)
        target_ms = int(max(0.0, src_t) * 1000)
        playing = self._play_timer.isActive()
        # Past the trim end? Keep it paused either way.
        past_end = src_t >= clip.src_end - 0.03
        if not playing:
            if self.player.playbackState() == QMediaPlayer.PlayingState:
                self.player.pause()
            # Still seek so the frame preview updates after loading /
            # splitting / undoing — just don't start playback.
            if abs(self.player.position() - target_ms) > self._SYNC_DRIFT_MS:
                self.player.setPosition(target_ms)
            return
        if past_end:
            if self.player.playbackState() == QMediaPlayer.PlayingState:
                self.player.pause()
            return
        if self.player.playbackState() != QMediaPlayer.PlayingState:
            self.player.setPosition(target_ms)
            self.player.play()
        elif abs(self.player.position() - target_ms) > self._SYNC_DRIFT_MS:
            self.player.setPosition(target_ms)

    def _pause_all_added_players(self) -> None:
        for player in self._added_players.values():
            if player.playbackState() == QMediaPlayer.PlayingState:
                player.pause()

    def _current_preview_clip(self) -> Clip | None:
        return next((c for c in self._clips if c.id == self._preview_clip_id), None)

    def _update_audio_volumes(self) -> None:
        clip = self._current_preview_clip()
        clip_muted = clip is None or clip.muted
        # An unlinked clip's embedded audio is silenced on the main player;
        # the detached audio is routed through `clip_audio_player` instead
        # so it obeys `audio_offset`.
        unlinked = (
            clip is not None
            and clip.asset.has_audio
            and not clip.linked_audio
        )
        has_added = bool(self._added_audios)
        clip_gain = clip.audio_volume if clip is not None else 1.0
        default_clip_vol = 0.0 if clip_muted else max(0.0, min(1.0, 0.7 * clip_gain))
        added_gain = max(0.0, min(1.0, self.audio_gain.value()))
        orig_vol = max(0.0, min(1.0, self.orig_gain.value() * clip_gain))

        if not has_added:
            if unlinked:
                self.audio.setVolume(0.0)
                self.clip_audio_output.setVolume(default_clip_vol)
            else:
                self.audio.setVolume(default_clip_vol)
                self.clip_audio_output.setVolume(0.0)
            for out in self._added_outputs.values():
                out.setVolume(0.0)
            return

        if self.audio_replace_cb.isChecked() or clip_muted:
            self.audio.setVolume(0.0)
            self.clip_audio_output.setVolume(0.0)
        elif unlinked:
            self.audio.setVolume(0.0)
            self.clip_audio_output.setVolume(orig_vol)
        else:
            self.audio.setVolume(orig_vol)
            self.clip_audio_output.setVolume(0.0)
        # Each added-audio item scales the global gain by its own volume.
        # QAudioOutput accepts 0.0-1.0, so anything above 100% is clamped in
        # preview only — export keeps the exact value.
        item_volumes = {a.id: a.volume for a in self._added_audios}
        for aid, out in self._added_outputs.items():
            item_vol = item_volumes.get(aid, 1.0)
            out.setVolume(max(0.0, min(1.0, added_gain * item_vol)))

    _SYNC_DRIFT_MS = 200

    def _sync_clip_audio_playback(self) -> None:
        """Position the unlinked-clip audio player relative to the clip's
        shifted range. Silent outside [start+offset, end+offset]."""
        clip = self._current_preview_clip()
        if (
            clip is None or not clip.asset.has_audio or clip.linked_audio
            or getattr(clip, "audio_removed", False)
        ):
            if self.clip_audio_player.playbackState() == QMediaPlayer.PlayingState:
                self.clip_audio_player.pause()
            return
        # Load the clip's file into the dedicated player if needed.
        src = QUrl.fromLocalFile(str(clip.path))
        if self.clip_audio_player.source() != src:
            self.clip_audio_player.setSource(src)
        if not self._play_timer.isActive():
            if self.clip_audio_player.playbackState() == QMediaPlayer.PlayingState:
                self.clip_audio_player.pause()
            return
        t = self.timeline.playhead()
        audio_start = clip.timeline_start + clip.audio_offset
        audio_end = clip.timeline_end + clip.audio_offset
        in_range = audio_start <= t < audio_end
        if in_range:
            src_t = clip.src_start + max(0.0, (t - audio_start)) * clip.speed
            target_ms = int(src_t * 1000)
            playing = (
                self.clip_audio_player.playbackState() == QMediaPlayer.PlayingState
            )
            if not playing:
                self.clip_audio_player.setPosition(target_ms)
                self.clip_audio_player.play()
            elif abs(self.clip_audio_player.position() - target_ms) > self._SYNC_DRIFT_MS:
                # Scrub landed us somewhere else — resync.
                self.clip_audio_player.setPosition(target_ms)
        else:
            if self.clip_audio_player.playbackState() == QMediaPlayer.PlayingState:
                self.clip_audio_player.pause()

    def _sync_added_audio_playback(self) -> None:
        """Walk every added-audio entry and start/pause its player based on
        whether the playhead is inside that entry's visible range."""
        if not self._is_playing():
            self._pause_all_added_players()
            return
        t = self.timeline.playhead()
        for audio in self._added_audios:
            player = self._added_players.get(audio.id)
            if player is None or audio.duration <= 0:
                continue
            start = audio.offset
            end = audio.timeline_end
            in_range = start <= t < end
            if in_range:
                src_t = audio.src_start + (t - start)
                target_ms = int(max(0.0, src_t) * 1000)
                playing = (
                    player.playbackState() == QMediaPlayer.PlayingState
                )
                if not playing:
                    player.setPosition(target_ms)
                    player.play()
                elif abs(player.position() - target_ms) > self._SYNC_DRIFT_MS:
                    player.setPosition(target_ms)
            else:
                if player.playbackState() == QMediaPlayer.PlayingState:
                    player.pause()

    def _is_playing(self) -> bool:
        """Playback state is driven entirely by `_play_timer`. Qt's media
        player state is not consulted — some backends eagerly flip into
        PlayingState on setSource, which would otherwise falsely kick
        aux audio players on and produce the "cursor frozen, audio playing,
        button says Play" bug."""
        return self._play_timer.isActive()

    def _on_media_status(self, status: QMediaPlayer.MediaStatus) -> None:
        if status != QMediaPlayer.LoadedMedia:
            return
        c = next((c for c in self._clips if c.id == self._preview_clip_id), None)
        if c is None:
            return
        # Re-seek to the playhead's frame once the media has finished
        # loading (backends can overwrite the earlier setPosition).
        t = self.timeline.playhead()
        src_t = c.src_for_timeline(t)
        self.player.setPosition(int(max(0.0, src_t) * 1000))
        # Defensive: never let the player start playback outside the timer.
        if not self._play_timer.isActive():
            self.player.pause()

    def _on_timeline_playhead(self, t: float) -> None:
        # Keep the timecode display in step no matter how the playhead
        # moved — scrub, keyboard step, Home/End, etc. all route here.
        self._update_timecode_display()
        # User-driven playhead changes pause playback — otherwise the timer
        # races the scrub and the preview ends up lagging whatever the user
        # just clicked on.
        if self._play_timer.isActive():
            self._play_timer.stop()
            self.player.pause()
            self._pause_all_added_players()
            self.clip_audio_player.pause()
            _swap_btn_icon(self.play_btn, "play")
        c = clip_at_timeline(self._clips, t)
        if c is None:
            # Playhead moved into a gap or past the last clip — hide the
            # video item so the preview goes black instead of freezing on
            # a stale frame.
            self.video_view.set_video_visible(False)
            self.video_view.hide_image()
            if self.player.playbackState() == QMediaPlayer.PlayingState:
                self.player.pause()
            self._sync_added_audio_playback()
            self._sync_clip_audio_playback()
            self._refresh_subtitle_overlay()
            return
        if c.id != self._preview_clip_id:
            self._set_preview_clip(c)
            self.timeline.select_clip(c.id)
        if c.asset.kind == "image":
            # Image preview is already attached in _set_preview_clip; no
            # seek to perform on the media player. Still sync added audio.
            self._sync_added_audio_playback()
            self._refresh_subtitle_overlay()
            return
        self.video_view.set_video_visible(True)
        src_t = c.src_for_timeline(t)
        target_ms = int(src_t * 1000)
        now = time.monotonic()
        # Throttle: if we seeked recently, buffer the target and flush on a
        # short delay so the final position always lands.
        if now - self._last_seek_wall < 0.06:
            self._pending_seek_ms = target_ms
            if not self._seek_throttle_timer.isActive():
                self._seek_throttle_timer.start(80)
        else:
            self.player.setPosition(target_ms)
            self._last_seek_wall = now
            self._pending_seek_ms = None
        self._sync_added_audio_playback()
        self._sync_clip_audio_playback()
        self._refresh_subtitle_overlay()

    def _flush_pending_seek(self) -> None:
        if self._pending_seek_ms is None:
            return
        self.player.setPosition(self._pending_seek_ms)
        self._last_seek_wall = time.monotonic()
        self._pending_seek_ms = None

    def _update_range_label(self) -> None:
        total = sequence_length(self._clips)
        self.range_label.setText(f"Sequence: {_fmt(total)}  •  {len(self._clips)} clip(s)")

    def _update_timecode_display(self) -> None:
        """Refresh the timecode box from the timeline's playhead. Skipped
        when the user has focus in the edit — we don't want to overwrite
        what they're typing."""
        if self.timecode_edit.hasFocus():
            return
        text = _format_timecode(self.timeline.playhead())
        if self.timecode_edit.text() != text:
            self.timecode_edit.setText(text)

    def _on_timecode_edited(self) -> None:
        parsed = _parse_timecode(self.timecode_edit.text())
        if parsed is None:
            # Roll back to the live playhead so the box never shows garbage.
            self.timecode_edit.setText(_format_timecode(self.timeline.playhead()))
            self.status.showMessage(
                "Unrecognized timecode. Try `1:23.456` or `0:01:23.456`.", 3500,
            )
            return
        self.timeline.set_playhead(parsed)
        # Normalize the display to the canonical format.
        self.timecode_edit.setText(_format_timecode(self.timeline.playhead()))
        self.timecode_edit.clearFocus()

    # --- crop ---------------------------------------------------------

    def _on_crop_toggled(self, checked: bool) -> None:
        c = self._selected_clip()
        if checked and not c:
            self.crop_btn.setChecked(False)
            return
        if checked and c:
            self.crop_overlay.set_video_aspect(c.asset.width / max(1, c.asset.height))
            if self.crop_overlay.normalized_rect() == QRectF(0, 0, 1, 1):
                self.crop_overlay.set_normalized_rect(QRectF(0.1, 0.1, 0.8, 0.8))
        self.crop_overlay.setVisible(checked)
        self.crop_overlay.raise_()
        self.crop_reset_btn.setVisible(checked)

    def _on_crop_reset(self) -> None:
        self.crop_overlay.reset()

    # --- preview context menu ----------------------------------------

    def _show_preview_menu(self, global_pos) -> None:  # noqa: ANN001
        menu = QMenu(self)
        extract_act = menu.addAction("Extract frame as JPG…")
        has_clip = self._current_preview_clip() is not None
        extract_act.setEnabled(has_clip)
        chosen = menu.exec(global_pos)
        if chosen is extract_act and has_clip:
            self._extract_current_frame()

    def _extract_current_frame(self) -> None:
        c = self._current_preview_clip()
        if c is None:
            return
        src_t = self.player.position() / 1000.0
        # Default filename: <clip>-frame-<hh_mm_ss>.jpg
        stamp = int(round(src_t))
        minutes, seconds = divmod(stamp, 60)
        hours, minutes = divmod(minutes, 60)
        suggested = str(
            c.path.with_name(f"{c.path.stem}-frame-{hours:02d}_{minutes:02d}_{seconds:02d}.jpg")
        )
        out_path, _ = QFileDialog.getSaveFileName(
            self, "Save frame as…", suggested, "JPEG (*.jpg *.jpeg);;PNG (*.png);;All files (*)",
        )
        if not out_path:
            return
        try:
            ff.extract_frame_full(c.path, src_t, Path(out_path))
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Extract failed", str(exc))
            return
        self.status.showMessage(f"Saved frame → {Path(out_path).name}", 5000)

    def _crop_pixels(self) -> tuple[int, int, int, int] | None:
        c = self._selected_clip()
        if not self.crop_btn.isChecked() or c is None:
            return None
        r = self.crop_overlay.normalized_rect()
        if r == QRectF(0, 0, 1, 1):
            return None
        sw, sh = c.asset.width, c.asset.height
        x = int(round(r.x() * sw)); y = int(round(r.y() * sh))
        w = int(round(r.width() * sw)); h = int(round(r.height() * sh))
        w -= w % 2; h -= h % 2
        x = max(0, min(sw - w, x - x % 2))
        y = max(0, min(sh - h, y - y % 2))
        if w < 2 or h < 2:
            return None
        return (x, y, w, h)

    # --- added audio ---------------------------------------------------

    def _on_added_audio_dropped(
        self, path: str, drop_t: float = -1.0, lane: int = 1,
    ) -> None:
        self._append_added_audio(
            Path(path), initial_offset=float(drop_t), lane=int(lane),
        )

    def _set_added_audio(self, path: Path) -> None:
        """Historical alias kept for callers that treat dropping audio as
        picking a single mix track. Appends to Audio Track 2 by default."""
        self._append_added_audio(path)

    def _on_added_audio_offset_changed(self, audio_id: str, offset: float) -> None:
        audio = next((a for a in self._added_audios if a.id == audio_id), None)
        if audio is None or abs(audio.offset - offset) < 1e-4:
            return
        self._snapshot()
        audio.offset = max(0.0, float(offset))
        self._sync_added_audio_playback()
        self._update_controls_enabled()

    def _on_added_audio_volume_requested(self, audio_id: str) -> None:
        """Edit one added-audio item's own gain. Cancelling, or accepting the
        value it already has, leaves the undo history untouched."""
        audio = next((a for a in self._added_audios if a.id == audio_id), None)
        if audio is None:
            return
        current = int(round(audio.volume * 100))
        pct, ok = QInputDialog.getInt(
            self, "Audio Volume", "Volume (%):", current, 0, 200, 5,
        )
        if not ok or pct == current:
            return
        self._snapshot()
        audio.volume = pct / 100.0
        self._refresh_added_audio_display()
        self._update_audio_volumes()
        self.status.showMessage(f"Added audio volume set to {pct}%.", 3000)

    def _on_added_audio_range_changed(self, audio_id: str) -> None:
        self._snapshot()
        tl_audio = next(
            (a for a in self.timeline._added_audios if a.id == audio_id), None,
        )
        app_audio = next(
            (a for a in self._added_audios if a.id == audio_id), None,
        )
        if tl_audio is not None and app_audio is not None:
            app_audio.src_start = tl_audio.src_start
            app_audio.src_end = tl_audio.src_end
            app_audio.offset = tl_audio.offset
        self._refresh_added_audio_display()
        self._sync_added_audio_playback()
        self._update_controls_enabled()

    def _on_added_audio_delete_requested(self, audio_id: str) -> None:
        if audio_id:
            self._remove_added_audio(audio_id)
        else:
            self._clear_all_added_audio()

    def _append_added_audio(
        self, path: Path, initial_offset: float = -1.0, lane: int = 1,
    ) -> None:
        if not path.exists():
            return
        try:
            dur = ff.probe_audio_duration(path)
        except Exception:  # noqa: BLE001
            dur = 0.0
        self._snapshot()
        # Drop-at-cursor when an offset is passed; otherwise append after the
        # last entry on the same lane for a natural back-to-back layout.
        if initial_offset >= 0.0:
            offset = initial_offset
        else:
            offset = max(
                (a.timeline_end for a in self._added_audios if a.lane == lane),
                default=0.0,
            )
        audio = AddedAudio(path=path, duration=dur, offset=offset, lane=int(lane))
        self._added_audios.append(audio)
        self._create_added_player(audio)
        self._kick_off_added_waveform(audio.id, path)
        self._refresh_added_audio_display()
        self._update_audio_volumes()
        self._update_controls_enabled()
        if self._play_timer.isActive():
            self._sync_added_audio_playback()
        self.status.showMessage(f"Added audio: {path.name}", 5000)

    def _remove_added_audio(self, audio_id: str) -> None:
        if not any(a.id == audio_id for a in self._added_audios):
            return
        self._snapshot()
        self._destroy_added_player(audio_id)
        self._added_audios = [a for a in self._added_audios if a.id != audio_id]
        self._refresh_added_audio_display()
        self._update_audio_volumes()
        self._update_controls_enabled()
        if not self._clips and not self._added_audios and self._play_timer.isActive():
            self._toggle_play()
        self.status.showMessage("Audio clip removed.", 2500)

    def _clear_all_added_audio(self) -> None:
        if not self._added_audios:
            return
        self._snapshot()
        for aid in list(self._added_players.keys()):
            self._destroy_added_player(aid)
        self._added_audios = []
        self._refresh_added_audio_display()
        self._update_audio_volumes()
        self._update_controls_enabled()
        if not self._clips and self._play_timer.isActive():
            self._toggle_play()
        self.status.showMessage("All added audio removed.", 3000)

    def _create_added_player(self, audio: AddedAudio) -> None:
        player = QMediaPlayer(self)
        output = QAudioOutput(self)
        output.setVolume(0.0)
        player.setAudioOutput(output)
        player.setSource(QUrl.fromLocalFile(str(audio.path)))
        self._added_players[audio.id] = player
        self._added_outputs[audio.id] = output

    def _destroy_added_player(self, audio_id: str) -> None:
        player = self._added_players.pop(audio_id, None)
        output = self._added_outputs.pop(audio_id, None)
        if player is not None:
            player.stop()
            player.setSource(QUrl())
            player.deleteLater()
        if output is not None:
            output.deleteLater()
        worker = self._added_wave_workers.pop(audio_id, None)
        if worker is not None:
            try:
                worker.cancel()
            except Exception:  # noqa: BLE001
                pass
        thread = self._added_wave_threads.pop(audio_id, None)
        if thread is not None and thread.isRunning():
            thread.quit()
            thread.wait(500)

    def _restore_added_audios(self, audios: list) -> None:
        """Rebuild the added-audio state from an undo snapshot. Tears down
        players for entries no longer present and creates players for new
        ones. Existing entries have their offsets/metadata refreshed in
        place."""
        new_ids = {a.id for a in audios}
        for aid in list(self._added_players.keys()):
            if aid not in new_ids:
                self._destroy_added_player(aid)
        cloned = [a.clone() for a in audios]
        existing_ids = set(self._added_players.keys())
        self._added_audios = cloned
        for a in cloned:
            if a.id not in existing_ids:
                self._create_added_player(a)

    def _on_audio_link_toggled(self, clip_id: str) -> None:
        c = next((cc for cc in self._clips if cc.id == clip_id), None)
        if c is None:
            return
        self._snapshot()
        if c.audio_removed:
            # Clicking the chip on a removed-audio clip restores it.
            c.audio_removed = False
            c.linked_audio = True
            c.audio_offset = 0.0
            self._hard_reset_clip_audio_player()
            self.timeline.set_clips(self._clips)
            self._update_audio_volumes()
            self._sync_clip_audio_playback()
            self.status.showMessage("Audio restored.", 3000)
            return
        c.linked_audio = not c.linked_audio
        if c.linked_audio:
            # Relinking: drop the separate audio player cold so it doesn't
            # keep pumping at the previous offset while the main player also
            # plays the embedded track (the "faulty restore" doubling bug).
            c.audio_offset = 0.0
            self._hard_reset_clip_audio_player()
        self.timeline.set_clips(self._clips)
        self._update_audio_volumes()
        self._sync_clip_audio_playback()
        if c.linked_audio:
            self.status.showMessage("Audio re-linked to clip.", 3500)
        else:
            self.status.showMessage(
                "Audio unlinked — drag along the audio track, or press Delete to remove.",
                5000,
            )

    def _hard_reset_clip_audio_player(self) -> None:
        """Fully stop the unlinked-clip audio player so a following relink
        doesn't leave its old playback state warm."""
        self.clip_audio_player.stop()
        self.clip_audio_player.setPosition(0)
        self.clip_audio_output.setVolume(0.0)

    def _on_added_audio_replace_toggled(self, replace: bool) -> None:
        if self.audio_replace_cb.isChecked() != replace:
            self._snapshot()
            self.audio_replace_cb.setChecked(replace)
            self._update_audio_volumes()
            self.status.showMessage(
                "Added audio will replace the clip audio." if replace
                else "Added audio will mix with the clip audio.",
                3500,
            )

    def _refresh_added_audio_display(self) -> None:
        self.timeline.set_added_audios(self._added_audios)

    def _kick_off_added_waveform(self, audio_id: str, path: Path) -> None:
        # Cancel any stale worker for this id first (shouldn't usually exist).
        prev = self._added_wave_workers.pop(audio_id, None)
        if prev is not None:
            try:
                prev.cancel()
            except Exception:  # noqa: BLE001
                pass
        thread, worker = start_waveform(audio_id, path)
        worker.finished.connect(self._on_added_waveform_ready, Qt.QueuedConnection)
        worker.failed.connect(self._on_added_waveform_error, Qt.QueuedConnection)
        thread.finished.connect(
            lambda aid=audio_id: self._added_wave_done(aid), Qt.QueuedConnection,
        )
        self._added_wave_threads[audio_id] = thread
        self._added_wave_workers[audio_id] = worker
        thread.start()

    def _on_added_waveform_ready(self, audio_id: str, peaks: list, rate: int) -> None:
        audio = next((a for a in self._added_audios if a.id == audio_id), None)
        if audio is None or not peaks:
            return
        audio.peaks = list(peaks)
        audio.rate = int(rate)
        self._refresh_added_audio_display()

    def _on_added_waveform_error(self, _audio_id: str, _msg: str) -> None:
        pass

    def _added_wave_done(self, audio_id: str) -> None:
        self._added_wave_threads.pop(audio_id, None)
        self._added_wave_workers.pop(audio_id, None)

    # --- export -------------------------------------------------------

    _AUDIO_FORMAT_ORDER = [
        "WAV (audio only)",
        "MP3 (audio only)",
        "Opus (audio only)",
        "FLAC (audio only)",
        "OGG (audio only)",
        "AAC (audio only)",
    ]

    def _populate_format_combo(self, audio_only: bool) -> None:
        previous_signal_state = self.format_combo.blockSignals(True)
        try:
            self.format_combo.clear()

            if audio_only:
                for key in self._AUDIO_FORMAT_ORDER:
                    if key in ff.EXPORT_FORMATS:
                        self.format_combo.addItem(key)
                self.format_combo.setCurrentText("WAV (audio only)")
            else:
                for key, spec in ff.EXPORT_FORMATS.items():
                    if spec["vcodec"] is not None:
                        self.format_combo.addItem(key)
                self.format_combo.setCurrentText("MP4 (H.264 + AAC)")
        finally:
            self.format_combo.blockSignals(previous_signal_state)

    def _on_export_type_changed(self, _text: str = "") -> None:
        self._populate_format_combo(
            self.export_type_combo.currentText() == "Audio Only"
        )
        self._update_controls_enabled()

    def _is_audio_only_export(self) -> bool:
        return self.export_type_combo.currentText() == "Audio Only"

    def _on_export_clicked(self) -> None:
        fmt_key = self.format_combo.currentText()
        spec = ff.EXPORT_FORMATS[fmt_key]
        is_audio_only = spec["vcodec"] is None
        if not self._clips and not (is_audio_only and self._added_audios):
            return
        ext = spec["ext"]
        if self._clips:
            suggested = str(self._clips[0].path.with_suffix(f".edited.{ext}"))
        else:
            suggested = str(self._added_audios[0].path.with_suffix(f".edited.{ext}"))
        out_path, _ = QFileDialog.getSaveFileName(
            self, "Export to…", suggested, f"{ext.upper()} (*.{ext});;All files (*)",
        )
        if not out_path:
            return

        audio_tracks: list[AudioTrack] = []
        replace = self.audio_replace_cb.isChecked()
        vol = self.audio_gain.value()
        orig_vol = self.orig_gain.value()
        for audio in self._added_audios:
            audio_tracks.append(
                AudioTrack(
                    path=audio.path,
                    replace=replace,
                    volume=audio.volume * vol,
                    original_volume=orig_vol,
                    offset=audio.offset,
                    duration=audio.src_span,
                    src_start=audio.src_start,
                )
            )

        region_start = region_end = None
        if self._region_export_range is not None:
            region_start, region_end = self._region_export_range

        active_sub = next((s.clone() for s in self._subs if s.active), None)

        # Resolution is a visual-only setting; audio-only jobs always leave
        # width/height unset.
        size = None if is_audio_only else self.resolution_combo.currentData()
        out_w, out_h = size if size else (None, None)

        job = ExportJob(
            clips=[c.clone() for c in self._clips],
            output=Path(out_path),
            fmt_key=fmt_key,
            crop=self._crop_pixels(),
            width=out_w,
            height=out_h,
            audio_tracks=audio_tracks,
            region_start=region_start,
            region_end=region_end,
            subtitles=active_sub,
        )

        self._last_progress = 0
        self._last_eta = None
        self.progress.setValue(0)
        self.progress.setFormat("starting…")
        self.export_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.export_log.clear()
        self.status.showMessage("Exporting…")

        thread, worker = start_export(job)
        worker.progress.connect(self._on_progress, Qt.QueuedConnection)
        worker.eta.connect(self._on_eta, Qt.QueuedConnection)
        worker.log.connect(self._on_worker_log, Qt.QueuedConnection)
        worker.finished.connect(self._on_export_done, Qt.QueuedConnection)
        worker.failed.connect(self._on_export_failed, Qt.QueuedConnection)
        thread.finished.connect(self._reset_after_export)
        self._export_thread = thread
        self._export_worker = worker
        self._update_controls_enabled()
        thread.start()

    def _on_cancel_clicked(self) -> None:
        if self._export_worker:
            self._export_worker.cancel()
            self.status.showMessage("Cancelling…")

    def _on_details_toggled(self, checked: bool) -> None:
        self._log_panel.setVisible(checked)
        self.details_btn.setText("Details ▾" if checked else "Details ▸")

    def _on_copy_log(self) -> None:
        QApplication.clipboard().setText(self.export_log.toPlainText())

    def _on_worker_log(self, msg: str) -> None:
        self.export_log.append(msg)
        # Show the first command line in status bar; suppress noisy stderr lines.
        if msg.startswith("$ "):
            self.status.showMessage(msg, 4000)

    def _on_progress(self, pct: int) -> None:
        self._last_progress = max(self._last_progress, pct)
        self.progress.setValue(self._last_progress)
        self._refresh_progress_text()

    def _on_eta(self, seconds: float) -> None:
        self._last_eta = seconds
        self._refresh_progress_text()

    def _refresh_progress_text(self) -> None:
        if self._last_progress >= 99 or self._last_eta is None:
            self.progress.setFormat("%p%")
        else:
            secs = max(0, int(round(self._last_eta)))
            m, s = divmod(secs, 60)
            self.progress.setFormat(f"%p%  •  ETA {m}:{s:02d}")

    def _on_export_done(self, out: Path) -> None:
        size_b = out.stat().st_size
        size, unit = (size_b / 1024, "KB")
        if size >= 1024:
            size, unit = (size / 1024, "MB")
        summary = f"Saved {out.name} ({size:.1f} {unit})"
        self.status.showMessage(summary, 8000)
        self.export_log.append(f"✓ {summary}")
        self._last_progress = 100
        self._last_eta = None
        self.progress.setValue(100)
        self.progress.setFormat("%p%")

    def _on_export_failed(self, msg: str) -> None:
        # First line as short status; full detail goes to the log panel.
        short = msg.splitlines()[0][:120] if msg else "Unknown error"
        self.status.showMessage(f"Failed: {short}", 8000)
        self.export_log.append(f"✗ {msg}")
        # Auto-expand Details so the user can see what went wrong.
        if not self.details_btn.isChecked():
            self.details_btn.setChecked(True)
            self._on_details_toggled(True)
        QMessageBox.warning(
            self, "Export failed",
            f"{short}\n\nSee Details for the full ffmpeg log.",
        )

    def _reset_after_export(self) -> None:
        self._export_thread = None
        self._export_worker = None
        self.cancel_btn.setEnabled(False)
        self._update_controls_enabled()

    # --- misc ---------------------------------------------------------

    def _update_controls_enabled(self) -> None:
        loaded = bool(self._clips)
        has_any = loaded or bool(self._added_audios)
        self.play_btn.setEnabled(has_any)
        audio_only = self._is_audio_only_export()
        exporting = self._export_thread is not None
        can_export = export_controls_enabled(
            has_clips=loaded,
            has_added_audio=bool(self._added_audios),
            audio_only=audio_only,
            exporting=exporting,
        )
        # Resolution only applies to visual output: it needs visual timeline
        # content, a Project export, and no export in flight.
        self.resolution_combo.setEnabled(
            not audio_only
            and export_controls_enabled(
                has_clips=loaded,
                has_added_audio=bool(self._added_audios),
                audio_only=False,
                exporting=exporting,
            )
        )
        self.crop_btn.setEnabled(loaded)
        self.format_combo.setEnabled(can_export)
        self.export_btn.setEnabled(can_export)
        for w in (self.split_btn, self.delete_clip_btn):
            w.setEnabled(has_any)
        self.merge_btn.setEnabled(self._can_merge())
        for w in (self.zoom_slider, self.zoom_out_btn, self.zoom_in_btn):
            w.setEnabled(has_any)

    def _can_merge(self) -> bool:
        sel_audio_id = self.timeline._added_audio_selected_id
        if sel_audio_id:
            audio = next((a for a in self._added_audios if a.id == sel_audio_id), None)
            if audio is not None:
                return self._has_mergeable_audio_neighbour(audio)
        c = self._selected_clip() or (
            clip_at_timeline(self._clips, self.timeline.playhead())
            if self._clips else None
        )
        if c is not None:
            return bool(self._available_merge_directions(c))
        return False

    def _has_mergeable_audio_neighbour(self, audio: AddedAudio) -> bool:
        for a in self._added_audios:
            if a.id == audio.id or a.path != audio.path or a.lane != audio.lane:
                continue
            if (abs(audio.src_end - a.src_start) < 0.05
                    and abs(audio.timeline_end - a.offset) < 0.05):
                return True
            if (abs(a.src_end - audio.src_start) < 0.05
                    and abs(a.timeline_end - audio.offset) < 0.05):
                return True
        return False

    def closeEvent(self, event) -> None:  # noqa: ANN001
        for d in (self._thumb_workers, self._wave_workers, self._added_wave_workers):
            for worker in list(d.values()):
                try:
                    worker.cancel()
                except Exception:  # noqa: BLE001
                    pass
        if self._export_worker is not None:
            try:
                self._export_worker.cancel()
            except Exception:  # noqa: BLE001
                pass
        if self._download_dialog is not None:
            self._download_dialog.cancel_and_wait()
            self._download_dialog.close()
        for d in (self._thumb_threads, self._wave_threads, self._added_wave_threads):
            for thread in list(d.values()):
                if thread.isRunning():
                    thread.quit(); thread.wait(1500)
        if self._export_thread and self._export_thread.isRunning():
            self._export_thread.quit(); self._export_thread.wait(2000)
        super().closeEvent(event)

    def _check_ffmpeg(self) -> None:
        try:
            ff.require_ffmpeg()
            ff.require_ffprobe()
        except ff.FFmpegMissingError as exc:
            QMessageBox.critical(
                self, "Missing dependency",
                f"{exc}\n\nffmpeg and ffprobe should ship next to this application. "
                f"If you are running from source, install ffmpeg with your package "
                f"manager or drop the binaries next to the app.",
            )

    # --- auto updater ---------------------------------------------------

    def _check_for_updates_in_background(self) -> None:
        """Fire-and-forget GitHub releases poll. Silent on success/failure
        unless a newer version shows up — then `_on_update_available` fires."""
        if self._update_thread is not None:
            return
        thread, worker = updater.start_check(__version__)
        worker.updateAvailable.connect(self._on_update_available, Qt.QueuedConnection)
        thread.finished.connect(self._on_update_check_done, Qt.QueuedConnection)
        self._update_thread = thread
        self._update_worker = worker
        thread.start()

    def _on_update_check_done(self) -> None:
        self._update_thread = None
        self._update_worker = None

    def _on_update_available(self, info) -> None:
        if self._update_prompt_shown:
            return
        self._update_prompt_shown = True
        self._prompt_update(info)

    def _prompt_update(self, info) -> None:
        kind = updater.bundle_kind()
        can_auto_install = kind == "appimage" and bool(info.asset_url)

        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Information)
        msg.setWindowTitle("Cove Video Editor — update available")
        msg.setText(
            f"Cove Video Editor v{info.latest_version} is available.\n"
            f"You're running v{__version__}.",
        )
        if can_auto_install:
            msg.setInformativeText(
                f"{info.asset_name} ({info.asset_size // (1024 * 1024)} MB). "
                "The app will restart after the update.",
            )
            install_btn = msg.addButton("Update now", QMessageBox.AcceptRole)
            open_btn = msg.addButton("View release", QMessageBox.HelpRole)
            msg.addButton("Later", QMessageBox.RejectRole)
        else:
            msg.setInformativeText(
                "Open the release page to download the latest installer.",
            )
            install_btn = None
            open_btn = msg.addButton("View release", QMessageBox.AcceptRole)
            msg.addButton("Later", QMessageBox.RejectRole)
        msg.exec()
        clicked = msg.clickedButton()
        if install_btn is not None and clicked is install_btn:
            self._install_update(info)
        elif open_btn is not None and clicked is open_btn:
            _open_url(info.release_url)

    def _install_update(self, info) -> None:
        if not info.asset_url:
            _open_url(info.release_url)
            return
        from .portable import is_portable, portable_data_dir
        if is_portable():
            cache = Path(portable_data_dir("cove-video-editor")) / "cache"
        else:
            cache = Path(os.path.expanduser("~/.cache/cove-video-editor"))
        cache.mkdir(parents=True, exist_ok=True)
        dest = cache / info.asset_name

        self._update_progress = QProgressDialog(
            f"Downloading {info.asset_name}…", "Cancel", 0, 100, self,
        )
        self._update_progress.setWindowTitle("Updating Cove Video Editor")
        self._update_progress.setAutoClose(False)
        self._update_progress.setAutoReset(False)
        self._update_progress.setMinimumDuration(0)
        self._update_progress.setValue(0)

        thread, worker = updater.start_download(info.asset_url, dest)
        self._update_progress.canceled.connect(worker.cancel)
        worker.progress.connect(self._update_progress.setValue, Qt.QueuedConnection)
        worker.finished.connect(self._on_update_downloaded, Qt.QueuedConnection)
        worker.failed.connect(self._on_update_download_failed, Qt.QueuedConnection)
        thread.finished.connect(self._on_update_download_thread_done, Qt.QueuedConnection)
        self._update_download_thread = thread
        self._update_download_worker = worker
        thread.start()

    def _on_update_downloaded(self, path: str) -> None:
        self._update_progress.close()
        try:
            new_path = updater.swap_in_appimage(Path(path))
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(
                self, "Update failed",
                f"Couldn't swap in the new AppImage:\n{exc}",
            )
            return
        updater.relaunch(new_path)
        QApplication.instance().quit()

    def _on_update_download_failed(self, msg: str) -> None:
        self._update_progress.close()
        QMessageBox.warning(
            self, "Update failed",
            f"The download didn't complete:\n{msg}",
        )

    def _on_update_download_thread_done(self) -> None:
        self._update_download_thread = None
        self._update_download_worker = None


def _fmt(seconds: float) -> str:
    seconds = max(0.0, seconds)
    m = int(seconds // 60)
    s = seconds - m * 60
    return f"{m:02d}:{s:05.2f}"


def _format_timecode(seconds: float) -> str:
    """Render a playhead time as ``H:MM:SS.mmm`` — VideoPad's readout format."""
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:06.3f}"


def _parse_timecode(raw: str) -> float | None:
    """Parse a user-typed timecode. Accepts any of:

    * ``12.345`` — raw seconds
    * ``MM:SS.mmm`` — minutes + seconds (e.g. ``1:23.456``)
    * ``H:MM:SS.mmm`` — hours + minutes + seconds (e.g. ``0:01:23.456``)

    Returns ``None`` when the input can't be parsed so the caller can
    leave the playhead alone and restore the display."""
    raw = raw.strip()
    if not raw:
        return None
    parts = raw.split(":")
    try:
        if len(parts) == 1:
            return max(0.0, float(parts[0]))
        if len(parts) == 2:
            return max(0.0, int(parts[0]) * 60 + float(parts[1]))
        if len(parts) == 3:
            return max(
                0.0,
                int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2]),
            )
    except ValueError:
        return None
    return None


class ClipPropertiesDialog(QDialog):
    """Small modal shown on double-click / right-click → Properties.

    Edits speed, source trim bounds, and per-clip mute. Values are applied to
    the live Clip object only after the user accepts; cancel discards.
    """

    def __init__(self, clip: Clip, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Clip properties")
        self._clip = clip
        self._accepted = False
        self._result: dict | None = None

        lay = QVBoxLayout(self)
        header = QLabel(
            f"<b>{clip.path.name}</b><br>"
            f"<span style='color:#9aa0ad'>{clip.asset.width}×{clip.asset.height}"
            f"  •  source {_fmt(clip.asset.duration)}</span>"
        )
        header.setTextFormat(Qt.RichText)
        lay.addWidget(header)

        form = QFormLayout()
        self.speed = QDoubleSpinBox()
        self.speed.setRange(0.25, 4.0); self.speed.setSingleStep(0.25)
        self.speed.setDecimals(2); self.speed.setSuffix("x")
        self.speed.setValue(clip.speed)
        form.addRow("Speed", self.speed)

        self.trim_start = QDoubleSpinBox()
        self.trim_start.setRange(0.0, clip.asset.duration)
        self.trim_start.setDecimals(3); self.trim_start.setSuffix(" s")
        self.trim_start.setValue(clip.src_start)
        form.addRow("Trim start", self.trim_start)

        self.trim_end = QDoubleSpinBox()
        self.trim_end.setRange(0.0, clip.asset.duration)
        self.trim_end.setDecimals(3); self.trim_end.setSuffix(" s")
        self.trim_end.setValue(clip.src_end)
        form.addRow("Trim end", self.trim_end)

        self.muted = QCheckBox("Mute this clip's audio")
        self.muted.setChecked(clip.muted)
        form.addRow("", self.muted)

        self.audio_volume = QSpinBox()
        self.audio_volume.setRange(0, 200)
        self.audio_volume.setSingleStep(5)
        self.audio_volume.setSuffix("%")
        self.audio_volume.setValue(int(round(clip.audio_volume * 100)))
        self.audio_volume.setEnabled(clip.asset.has_audio)
        form.addRow("Volume", self.audio_volume)

        lay.addLayout(form)

        row = QHBoxLayout()
        self.reset_btn = QPushButton("Reset trim")
        self.reset_btn.clicked.connect(self._reset_trim)
        row.addWidget(self.reset_btn)
        row.addStretch(1)
        lay.addLayout(row)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def _reset_trim(self) -> None:
        self.trim_start.setValue(0.0)
        self.trim_end.setValue(self._clip.asset.duration)

    def accept(self) -> None:
        s, e = self.trim_start.value(), self.trim_end.value()
        if e <= s + 0.01:
            QMessageBox.information(self, "Invalid trim", "End must be after start.")
            return
        self._result = {
            "speed": self.speed.value(),
            "src_start": s,
            "src_end": e,
            "muted": self.muted.isChecked(),
            "audio_volume": self.audio_volume.value() / 100.0,
        }
        super().accept()

    def result_values(self) -> dict | None:
        return self._result


class SubtitleStyleDialog(QDialog):
    """Edit font size, colors, outline and position for the active subtitle
    track. Every widget change emits ``stylePreview`` so the caller can
    update the live overlay in real time — the user sees their edits
    before committing. Accepting returns the final dict; cancelling leaves
    it to the caller to revert from their saved snapshot."""

    stylePreview = Signal(dict)

    def __init__(self, sub: SubtitleTrack, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Subtitle style — {sub.path.name}")
        self._sub = sub
        self._result: dict | None = None
        self._primary_color = sub.primary_color
        self._outline_color = sub.outline_color

        lay = QVBoxLayout(self)
        form = QFormLayout()

        self.font_family = QComboBox()
        choices = available_subtitle_fonts()
        for fam in choices:
            self.font_family.addItem(fam)
        current = sub.font_family if sub.font_family in choices else choices[0]
        self.font_family.setCurrentText(current)
        self.font_family.setToolTip(
            "Typeface used in both the live preview and the exported "
            "burn-in. Only fonts installed on this system are listed."
        )
        form.addRow("Font", self.font_family)

        self.font_size = QSpinBox()
        self.font_size.setRange(10, 120)
        self.font_size.setValue(sub.font_size)
        self.font_size.setSuffix(" px")
        self.font_size.setToolTip(
            "Font pixel height at the video's native resolution. 32–48 "
            "works for most 1080p; go larger for 4K or smaller mobile clips. "
            "Preview and export stay in sync — no more guess-then-re-export."
        )
        form.addRow("Font size", self.font_size)

        self.primary_btn = QPushButton()
        self._paint_color_button(self.primary_btn, self._primary_color)
        self.primary_btn.clicked.connect(self._pick_primary)
        form.addRow("Text color", self.primary_btn)

        self.outline_btn = QPushButton()
        self._paint_color_button(self.outline_btn, self._outline_color)
        self.outline_btn.clicked.connect(self._pick_outline)
        form.addRow("Outline color", self.outline_btn)

        self.outline = QSpinBox()
        self.outline.setRange(0, 8)
        self.outline.setValue(sub.outline)
        form.addRow("Outline width", self.outline)

        self.position = QComboBox()
        self.position.addItem("Bottom")
        self.position.addItem("Top")
        self.position.setCurrentText(sub.position.capitalize())
        form.addRow("Position", self.position)

        lay.addLayout(form)

        hint = QLabel(
            "Style updates the preview live. Scrub the timeline to a cue "
            "to see it overlaid."
        )
        hint.setStyleSheet("color:#9aa0ad; font-size:11px;")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

        # Live preview: re-emit whenever anything changes.
        self.font_family.currentTextChanged.connect(self._emit_preview)
        self.font_size.valueChanged.connect(self._emit_preview)
        self.outline.valueChanged.connect(self._emit_preview)
        self.position.currentTextChanged.connect(self._emit_preview)

    # --- helpers ------------------------------------------------------

    def _current_values(self) -> dict:
        return {
            "font_family": self.font_family.currentText(),
            "font_size": self.font_size.value(),
            "primary_color": self._primary_color,
            "outline_color": self._outline_color,
            "outline": self.outline.value(),
            "position": self.position.currentText().lower(),
        }

    def _emit_preview(self, *_args) -> None:
        self.stylePreview.emit(self._current_values())

    def _pick_primary(self) -> None:
        color = QColorDialog.getColor(
            initial=_parse_qcolor(self._primary_color), parent=self,
            title="Text color",
        )
        if color.isValid():
            self._primary_color = color.name(QColor.HexRgb).upper()
            self._paint_color_button(self.primary_btn, self._primary_color)
            self._emit_preview()

    def _pick_outline(self) -> None:
        color = QColorDialog.getColor(
            initial=_parse_qcolor(self._outline_color), parent=self,
            title="Outline color",
        )
        if color.isValid():
            self._outline_color = color.name(QColor.HexRgb).upper()
            self._paint_color_button(self.outline_btn, self._outline_color)
            self._emit_preview()

    @staticmethod
    def _paint_color_button(btn: QPushButton, color_hex: str) -> None:
        btn.setText(color_hex)
        btn.setStyleSheet(
            f"QPushButton {{ background:{color_hex}; color:{_contrast_text(color_hex)};"
            f" border:1px solid #39404d; border-radius:4px; padding:4px 10px; }}"
        )

    def accept(self) -> None:
        self._result = self._current_values()
        super().accept()

    def result_values(self) -> dict | None:
        return self._result


class SubtitleSyncDialog(QDialog):
    """Nudge the active subtitle's offset with a spinbox + slider. Opened
    modeless so the main window stays playable — scrub, hit Space to
    play, drag the slider, and watch the overlay move in real time."""

    offsetPreview = Signal(int)          # offset in ms (may be negative)

    def __init__(
        self, sub: SubtitleTrack, parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Sync subtitles — {sub.path.name}")
        self._saved = sub.offset_ms

        lay = QVBoxLayout(self)
        blurb = QLabel(
            "Drag the slider while the video plays — the live overlay "
            "shifts as you move. OK keeps the offset, Cancel reverts."
        )
        blurb.setWordWrap(True)
        blurb.setStyleSheet("color:#cfd0d4;")
        lay.addWidget(blurb)

        form = QFormLayout()
        self.offset_spin = QDoubleSpinBox()
        self.offset_spin.setRange(-30.0, 30.0)
        self.offset_spin.setDecimals(2)
        self.offset_spin.setSingleStep(0.05)
        self.offset_spin.setSuffix(" s")
        self.offset_spin.setValue(sub.offset_ms / 1000.0)
        form.addRow("Offset", self.offset_spin)

        self.offset_slider = QSlider(Qt.Horizontal)
        # ±5 s range by default, 10 ms granularity → 1000 steps each side.
        self.offset_slider.setRange(-500, 500)
        self.offset_slider.setValue(int(round(sub.offset_ms / 10)))
        self.offset_slider.setTickInterval(50)
        self.offset_slider.setTickPosition(QSlider.TicksBelow)
        form.addRow("Fine", self.offset_slider)

        lay.addLayout(form)

        hint = QLabel(
            "Tip: press Space in the main window to play/pause without "
            "closing this dialog."
        )
        hint.setStyleSheet("color:#9aa0ad; font-size:11px;")
        lay.addWidget(hint)

        row = QHBoxLayout()
        self.reset_btn = QPushButton("Reset to 0")
        self.reset_btn.clicked.connect(lambda: self.set_offset_ms(0))
        row.addWidget(self.reset_btn)
        row.addStretch(1)
        lay.addLayout(row)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

        self._syncing = False
        self.offset_spin.valueChanged.connect(self._on_spin_changed)
        self.offset_slider.valueChanged.connect(self._on_slider_changed)

    def _on_spin_changed(self, val: float) -> None:
        if self._syncing:
            return
        self._syncing = True
        self.offset_slider.setValue(int(round(val * 100)))
        self._syncing = False
        self.offsetPreview.emit(int(round(val * 1000)))

    def _on_slider_changed(self, val: int) -> None:
        if self._syncing:
            return
        self._syncing = True
        self.offset_spin.setValue(val / 100.0)
        self._syncing = False
        self.offsetPreview.emit(int(round(val * 10)))

    def set_offset_ms(self, ms: int) -> None:
        self._syncing = True
        self.offset_spin.setValue(ms / 1000.0)
        self.offset_slider.setValue(int(round(ms / 10)))
        self._syncing = False
        self.offsetPreview.emit(int(ms))

    def current_offset_ms(self) -> int:
        return int(round(self.offset_spin.value() * 1000))


def _parse_qcolor(hexstr: str) -> QColor:
    c = QColor(hexstr)
    if not c.isValid():
        return QColor("#FFFFFF")
    return c


def _contrast_text(hex_rgb: str) -> str:
    """Return black or white depending on perceived brightness of `hex_rgb`."""
    c = _parse_qcolor(hex_rgb)
    # ITU-R BT.601 luma.
    luma = 0.299 * c.redF() + 0.587 * c.greenF() + 0.114 * c.blueF()
    return "#000000" if luma > 0.55 else "#FFFFFF"
