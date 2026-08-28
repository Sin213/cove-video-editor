from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QKeyEvent, QMouseEvent, QPainter, QPen
from PySide6.QtWidgets import QWidget


HANDLE_SIZE = 10
HIT_PAD = 14
MIN_NORMALIZED = 0.05

FREE_PRESET = "Free (Custom)"

#: Selectable crop aspect ratios, mapped to a target *pixel* aspect ratio.
#: ``None`` means no lock - handles resize freely.
CROP_ASPECT_PRESETS: dict[str, float | None] = {
    FREE_PRESET: None,
    "16:9 (Landscape / YouTube)": 16 / 9,
    "9:16 (TikTok / Reels / Shorts)": 9 / 16,
    "1:1 (Square / Instagram)": 1.0,
    "4:5 (Portrait / Social)": 4 / 5,
    "4:3 (Standard / Classic)": 4 / 3,
    "21:9 (Cinematic / Ultrawide)": 21 / 9,
}


def compact_preset_label(preset_name: str) -> str:
    """Short status tag for a committed preset display key.

    ``"9:16 (TikTok / Reels / Shorts)"`` becomes ``"9:16"``. Free and any
    unrecognised key become ``"Active"``: a hand-adjusted crop has no
    ratio worth naming, and inventing one from arbitrary text would lie.
    The registry above stays the single source of preset truth.
    """
    if CROP_ASPECT_PRESETS.get(preset_name) is None:
        return "Active"
    return preset_name.split(" ")[0]


class CropOverlay(QWidget):
    """Draggable crop rectangle in normalized 0..1 source coords.

    Renders on top of a video widget, accounts for letterboxing so the rect
    always tracks the actual video pixels rather than the widget area.

    The rect can be locked to a standard aspect ratio preset. Because the
    rect is normalized against the source, a target *pixel* aspect maps to
    a normalized ratio of ``target_aspect / source_aspect``.
    """

    cropChanged = Signal(QRectF)
    #: The user asked to apply the current draft. Purely an intent signal -
    #: the overlay owns no document state and commits nothing itself.
    confirmRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMouseTracking(True)
        # Return/Enter is an accelerator scoped to this widget rather than a
        # window-wide shortcut, so the timecode field keeps its own Return.
        self.setFocusPolicy(Qt.StrongFocus)
        self._video_aspect: float = 16 / 9
        self._aspect_lock: float | None = None
        self._preset_name: str = FREE_PRESET
        self._rect_norm: QRectF = QRectF(0.0, 0.0, 1.0, 1.0)
        self._drag_target: str | None = None
        self._drag_start_widget: QPointF | None = None
        self._drag_start_rect: QRectF | None = None

    def set_video_aspect(self, aspect: float) -> None:
        if aspect > 0:
            changed = abs(aspect - self._video_aspect) > 1e-9
            self._video_aspect = aspect
            if changed and self._aspect_lock is not None:
                # The normalized ratio depends on the source, so an active
                # lock has to be re-fitted or its effective pixel aspect
                # silently drifts to the wrong ratio. Only a genuine change
                # justifies it: callers re-announce the same aspect purely
                # to synchronise, and refitting there would throw away a
                # crop the user had already moved or resized.
                self._apply_max_area_rect()
            self.update()

    def set_aspect_ratio_preset(
        self, aspect_target: float | None, preset_name: str = "",
    ) -> None:
        """Lock the crop box to ``aspect_target`` (a pixel aspect), or unlock.

        Locking immediately fits a centered maximum-area rectangle at that
        ratio. Passing ``None`` clears the lock and leaves the current
        rectangle alone - clearing the lock is not a reset.
        """
        self._aspect_lock = aspect_target
        self._preset_name = preset_name or (
            FREE_PRESET if aspect_target is None else f"{aspect_target:.2f}"
        )
        if aspect_target is not None and self._video_aspect > 0:
            self._apply_max_area_rect()
        self.update()

    def aspect_ratio_preset(self) -> float | None:
        return self._aspect_lock

    def preset_name(self) -> str:
        """The display key of the draft's current preset."""
        return self._preset_name

    def aspect_badge_text(self) -> str | None:
        """Compact ratio tag for the on-canvas pill, or ``None`` when free."""
        if self._aspect_lock is None or self._preset_name == FREE_PRESET:
            return None
        return self._preset_name.split(" ")[0]

    def _max_area_rect(self, aspect_target: float) -> QRectF:
        """Largest centered rect at ``aspect_target`` inside the 0..1 source.

        Deliberately not floored at ``MIN_NORMALIZED``: on an extreme source
        (a very wide or very tall one) the largest rect at the requested
        ratio can be thinner than the minimum, and raising it would hand
        back a different aspect than the one the user picked.
        """
        norm_ar = aspect_target / self._video_aspect
        if norm_ar <= 1.0:
            h = 1.0
            w = norm_ar
        else:
            w = 1.0
            h = 1.0 / norm_ar
        return QRectF((1.0 - w) / 2.0, (1.0 - h) / 2.0, w, h)

    def _apply_max_area_rect(self) -> None:
        self._rect_norm = self._max_area_rect(self._aspect_lock)
        self.update()
        self.cropChanged.emit(self.normalized_rect())

    def set_normalized_rect(self, rect: QRectF) -> None:
        self._rect_norm = self._clamp(QRectF(rect))
        self.update()

    def normalized_rect(self) -> QRectF:
        return QRectF(self._rect_norm)

    def reset(self) -> None:
        self._aspect_lock = None
        self._preset_name = FREE_PRESET
        self._rect_norm = QRectF(0.0, 0.0, 1.0, 1.0)
        self.update()
        self.cropChanged.emit(self.normalized_rect())

    def fit_to_canvas(self) -> None:
        """Grow the crop box to the largest rect that still fits the source.

        With a preset locked this keeps the ratio and re-centres; in Free
        mode it means the whole frame. The mode itself is never changed.
        """
        if self._aspect_lock is not None and self._video_aspect > 0:
            self._apply_max_area_rect()
            return
        self._rect_norm = QRectF(0.0, 0.0, 1.0, 1.0)
        self.update()
        self.cropChanged.emit(self.normalized_rect())

    def _video_display_rect(self) -> QRectF:
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            return QRectF(0, 0, 0, 0)
        widget_aspect = w / h
        if widget_aspect > self._video_aspect:
            actual_h = float(h)
            actual_w = h * self._video_aspect
            x = (w - actual_w) / 2
            y = 0.0
        else:
            actual_w = float(w)
            actual_h = w / self._video_aspect
            x = 0.0
            y = (h - actual_h) / 2
        return QRectF(x, y, actual_w, actual_h)

    def _crop_rect_widget(self) -> QRectF:
        v = self._video_display_rect()
        n = self._rect_norm
        return QRectF(
            v.x() + n.x() * v.width(),
            v.y() + n.y() * v.height(),
            n.width() * v.width(),
            n.height() * v.height(),
        )

    def _handle_centers(self, c: QRectF) -> dict[str, QPointF]:
        cx = (c.left() + c.right()) / 2
        cy = (c.top() + c.bottom()) / 2
        return {
            "tl": QPointF(c.left(), c.top()),
            "tr": QPointF(c.right(), c.top()),
            "bl": QPointF(c.left(), c.bottom()),
            "br": QPointF(c.right(), c.bottom()),
            "t":  QPointF(cx, c.top()),
            "b":  QPointF(cx, c.bottom()),
            "l":  QPointF(c.left(), cy),
            "r":  QPointF(c.right(), cy),
        }

    def paintEvent(self, _event) -> None:  # noqa: ANN001
        if not self.isVisible():
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)

        v = self._video_display_rect()
        c = self._crop_rect_widget()

        dim = QColor(0, 0, 0, 150)
        if c.top() > v.top():
            p.fillRect(QRectF(v.left(), v.top(), v.width(), c.top() - v.top()), dim)
        if c.bottom() < v.bottom():
            p.fillRect(QRectF(v.left(), c.bottom(), v.width(), v.bottom() - c.bottom()), dim)
        p.fillRect(QRectF(v.left(), c.top(), c.left() - v.left(), c.height()), dim)
        p.fillRect(QRectF(c.right(), c.top(), v.right() - c.right(), c.height()), dim)

        thirds_pen = QPen(QColor(255, 255, 255, 80), 1, Qt.DashLine)
        p.setPen(thirds_pen)
        for i in (1, 2):
            x = c.left() + c.width() * i / 3
            p.drawLine(QPointF(x, c.top()), QPointF(x, c.bottom()))
            y = c.top() + c.height() * i / 3
            p.drawLine(QPointF(c.left(), y), QPointF(c.right(), y))

        border_pen = QPen(QColor("#5fb4ff"))
        border_pen.setWidth(2)
        p.setPen(border_pen)
        p.setBrush(Qt.NoBrush)
        p.drawRect(c)

        p.setBrush(QColor("#5fb4ff"))
        p.setPen(QPen(QColor("#0d1216"), 1))
        s = HANDLE_SIZE
        for pt in self._handle_centers(c).values():
            p.drawRect(QRectF(pt.x() - s / 2, pt.y() - s / 2, s, s))

        tag = self.aspect_badge_text()
        if tag:
            badge_font = QFont(p.font())
            badge_font.setBold(True)
            p.setFont(badge_font)
            tw = p.fontMetrics().horizontalAdvance(tag)
            badge = QRectF(c.left() + 8, c.top() + 8, tw + 14, 20)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(13, 18, 22, 210))
            p.drawRoundedRect(badge, 4, 4)
            p.setPen(QColor("#5fb4ff"))
            p.drawText(badge, Qt.AlignCenter, tag)

        p.end()

    def _hit_test(self, pos: QPointF) -> str | None:
        c = self._crop_rect_widget()
        for name, center in self._handle_centers(c).items():
            if (abs(pos.x() - center.x()) <= HIT_PAD
                    and abs(pos.y() - center.y()) <= HIT_PAD):
                return name
        if c.contains(pos):
            return "move"
        return None

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.LeftButton:
            return
        target = self._hit_test(event.position())
        if target:
            self._drag_target = target
            self._drag_start_widget = event.position()
            self._drag_start_rect = QRectF(self._rect_norm)
            event.accept()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        """Double-clicking the crop body is a shortcut for Confirm.

        Only the body counts. A double-click on a resize handle or outside
        the box is an ordinary sizing gesture, and a live drag target means
        the user is still adjusting - confirming there would apply a rect
        they were in the middle of changing.
        """
        if event.button() != Qt.LeftButton or self._drag_target is not None:
            return
        if self._hit_test(event.position()) == "move":
            self.confirmRequested.emit()
            event.accept()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.confirmRequested.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_target:
            self._apply_drag(event.position())
        else:
            self.setCursor(_cursor_for(self._hit_test(event.position())))

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._drag_target:
            self._drag_target = None
            self.cropChanged.emit(self.normalized_rect())

    def _apply_drag(self, pos: QPointF) -> None:
        v = self._video_display_rect()
        if v.width() <= 0 or v.height() <= 0 or self._drag_start_widget is None:
            return
        dx = (pos.x() - self._drag_start_widget.x()) / v.width()
        dy = (pos.y() - self._drag_start_widget.y()) / v.height()
        r = QRectF(self._drag_start_rect)
        target = self._drag_target

        if target == "move":
            r.translate(dx, dy)
        elif self._aspect_lock is not None and self._video_aspect > 0:
            r = self._locked_drag_rect(target, r, dx, dy)
        else:
            if "l" in target:
                r.setLeft(min(r.right() - MIN_NORMALIZED, r.left() + dx))
            if "r" in target:
                r.setRight(max(r.left() + MIN_NORMALIZED, r.right() + dx))
            if "t" in target:
                r.setTop(min(r.bottom() - MIN_NORMALIZED, r.top() + dy))
            if "b" in target:
                r.setBottom(max(r.top() + MIN_NORMALIZED, r.bottom() + dy))

        if self._aspect_lock is not None and self._video_aspect > 0:
            # The generic clamp raises a sub-minimum axis, which would
            # rewrite the locked ratio; the locked rect is built in-bounds,
            # so it only ever needs repositioning.
            self._rect_norm = self._clamp_bounds(r)
        else:
            self._rect_norm = self._clamp(r)
        self.update()

    def _clamp_bounds(self, r: QRectF) -> QRectF:
        """Slide ``r`` back inside 0..1 without touching its size."""
        return QRectF(
            min(max(0.0, r.left()), max(0.0, 1.0 - r.width())),
            min(max(0.0, r.top()), max(0.0, 1.0 - r.height())),
            r.width(), r.height(),
        )

    def _locked_drag_rect(
        self, target: str, orig: QRectF, dx: float, dy: float,
    ) -> QRectF:
        """Resize ``orig`` by a drag delta while holding the locked ratio.

        Corners anchor the opposite corner; edges anchor the opposite edge
        and keep the orthogonal centre put where the bounds allow it.
        """
        norm_ar = self._aspect_lock / self._video_aspect
        # Both normalized axes must stay at or above MIN_NORMALIZED, so the
        # floor lives on whichever axis hits it first. Deriving the other
        # from the ratio keeps a fully shrunk box on-ratio. On an extreme
        # source that floor can exceed what actually fits, and holding the
        # ratio wins over holding the minimum - otherwise the box leaves
        # the frame and gets reshaped by the clamp.
        min_w = min(
            max(MIN_NORMALIZED, MIN_NORMALIZED * norm_ar),
            min(1.0, norm_ar),
        )
        min_h = min_w / norm_ar

        if target in ("tl", "tr", "bl", "br"):
            grows_right = "r" in target
            grows_down = "b" in target
            # Follow whichever pointer axis moved further, in width terms.
            grow_x = dx if grows_right else -dx
            grow_y = dy if grows_down else -dy
            delta_w = (
                grow_x if abs(grow_x) >= abs(grow_y * norm_ar) else grow_y * norm_ar
            )
            max_w = min(
                (1.0 - orig.left()) if grows_right else orig.right(),
                ((1.0 - orig.top()) if grows_down else orig.bottom()) * norm_ar,
            )
            w = max(min_w, min(max_w, orig.width() + delta_w))
            h = w / norm_ar
            x = orig.left() if grows_right else orig.right() - w
            y = orig.top() if grows_down else orig.bottom() - h
            return QRectF(x, y, w, h)

        if target in ("l", "r"):
            grows_right = target == "r"
            # h = w / norm_ar must stay <= 1, hence the norm_ar width cap.
            max_w = min(
                (1.0 - orig.left()) if grows_right else orig.right(), norm_ar,
            )
            w = max(min_w, min(max_w, orig.width() + (dx if grows_right else -dx)))
            h = w / norm_ar
            cy = (orig.top() + orig.bottom()) / 2.0
            return QRectF(
                orig.left() if grows_right else orig.right() - w,
                max(0.0, min(1.0 - h, cy - h / 2.0)),
                w, h,
            )

        if target in ("t", "b"):
            grows_down = target == "b"
            max_h = min(
                (1.0 - orig.top()) if grows_down else orig.bottom(), 1.0 / norm_ar,
            )
            h = max(min_h, min(max_h, orig.height() + (dy if grows_down else -dy)))
            w = h * norm_ar
            cx = (orig.left() + orig.right()) / 2.0
            return QRectF(
                max(0.0, min(1.0 - w, cx - w / 2.0)),
                orig.top() if grows_down else orig.bottom() - h,
                w, h,
            )

        return QRectF(orig)

    def _clamp(self, r: QRectF) -> QRectF:
        if r.width() < MIN_NORMALIZED:
            r.setWidth(MIN_NORMALIZED)
        if r.height() < MIN_NORMALIZED:
            r.setHeight(MIN_NORMALIZED)
        if r.left() < 0:
            r.translate(-r.left(), 0)
        if r.top() < 0:
            r.translate(0, -r.top())
        if r.right() > 1:
            r.translate(1 - r.right(), 0)
        if r.bottom() > 1:
            r.translate(0, 1 - r.bottom())
        return QRectF(
            max(0.0, r.left()),
            max(0.0, r.top()),
            min(1.0 - max(0.0, r.left()), r.width()),
            min(1.0 - max(0.0, r.top()), r.height()),
        )


def _cursor_for(target: str | None) -> Qt.CursorShape:
    return {
        "move": Qt.SizeAllCursor,
        "tl": Qt.SizeFDiagCursor, "br": Qt.SizeFDiagCursor,
        "tr": Qt.SizeBDiagCursor, "bl": Qt.SizeBDiagCursor,
        "t": Qt.SizeVerCursor, "b": Qt.SizeVerCursor,
        "l": Qt.SizeHorCursor, "r": Qt.SizeHorCursor,
    }.get(target, Qt.ArrowCursor)
