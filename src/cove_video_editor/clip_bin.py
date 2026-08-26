"""Library / clip bin on the left side of the window.

Holds every imported media asset. Assets live on one of four tabs — Video
Files / Audio Files / Images / Subs — and are shown as thumbnail tiles.
Double-click (or drag onto the timeline) to add to the sequence. Drop files
from the OS to import.

In 2.0 the tab labels include live counts: e.g. ``Audio Files (1)``,
``Images (10)``. The count is suppressed when a tab is empty.
"""
from __future__ import annotations

from PySide6.QtCore import QMimeData, QPoint, QPointF, QRect, QSize, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QDrag,
    QDragEnterEvent,
    QDropEvent,
    QIcon,
    QPainter,
    QPen,
    QPixmap,
    QPolygonF,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import theme
from .clip import MediaAsset


ASSET_MIME = "application/x-cove-asset-id"

# How far the pointer may travel between press and release and still count
# as a click on empty space (rather than the start of a drag). Small enough
# that a real drag never opens the picker, loose enough that hand jitter on
# a click still does.
BROWSE_CLICK_SLOP_PX = 6


class AssetList(QListWidget):
    """Icon-grid list with drag-to-timeline AND drag-from-OS support."""

    deleteRequested = Signal(str)        # asset id
    filesDropped = Signal(list)          # list[str]
    browseRequested = Signal()           # empty area clicked - open a picker

    # Empty-state hint: both ways to get media in.
    EMPTY_HINT = ("Click to browse for files", "or drop files or folders here")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setViewMode(QListWidget.IconMode)
        self.setFlow(QListWidget.LeftToRight)
        self.setResizeMode(QListWidget.Adjust)
        self.setMovement(QListWidget.Static)
        self.setIconSize(QSize(120, 68))
        # Without an explicit grid, the first layout pass stretches the
        # single tile across the available row and squashes its height; fix
        # each cell at a stable thumb+label footprint.
        self.setGridSize(QSize(140, 106))
        self.setSpacing(10)
        self.setUniformItemSizes(True)
        self.setWordWrap(True)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        # `color` on :selected is load-bearing — without it the global
        # QSS `selection-color: ACCENT_INK` kicks in and the filename goes
        # near-black on the dark selection bg.
        self.setStyleSheet(
            f"QListWidget {{ background:{theme.PANEL}; border:none;"
            f" color:{theme.TEXT}; padding:6px; }}"
            f"QListWidget::item {{ padding:4px; border-radius:6px; color:{theme.TEXT}; }}"
            f"QListWidget::item:hover {{ background:#0f171b; color:{theme.TEXT}; }}"
            f"QListWidget::item:selected {{"
            f" background:#0f2a2e; border: 1px solid {theme.ACCENT};"
            f" color:{theme.TEXT}; }}"
        )
        self._drop_highlight = False
        self._press_on_empty = False
        self._press_pos = QPoint()

    # --- click empty area to browse -------------------------------------

    def mousePressEvent(self, event) -> None:  # noqa: ANN001
        # Remember where an empty-area left-press started so a plain click
        # (no drag) can open the file picker on release. Presses on an item,
        # or with any other button, never arm it.
        pos = event.position().toPoint()
        self._press_on_empty = (
            event.button() == Qt.LeftButton and self.itemAt(pos) is None
        )
        self._press_pos = pos
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: ANN001
        armed = self._press_on_empty
        # One release consumes the press either way, so a stray second
        # release cannot fire a second browse.
        self._press_on_empty = False
        pos = event.position().toPoint()
        if (
            armed
            and event.button() == Qt.LeftButton
            and self.itemAt(pos) is None
            and (pos - self._press_pos).manhattanLength() < BROWSE_CLICK_SLOP_PX
        ):
            self.browseRequested.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    # --- key handling ---------------------------------------------------

    def keyPressEvent(self, event) -> None:  # noqa: ANN001
        if event.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            item = self.currentItem()
            if item is not None:
                asset_id = item.data(Qt.UserRole)
                if asset_id:
                    self.deleteRequested.emit(str(asset_id))
                    event.accept()
                    return
        super().keyPressEvent(event)

    # --- outgoing drag (item -> timeline) -------------------------------

    def mimeData(self, items):  # noqa: ANN001
        md = QMimeData()
        if items:
            asset_id = items[0].data(Qt.UserRole)
            md.setData(ASSET_MIME, str(asset_id).encode())
            md.setText(str(items[0].text()))
        return md

    def startDrag(self, supported):  # noqa: ANN001
        item = self.currentItem()
        if not item:
            return
        drag = QDrag(self)
        md = QMimeData()
        md.setData(ASSET_MIME, str(item.data(Qt.UserRole)).encode())
        md.setText(item.text())
        drag.setMimeData(md)
        icon = item.icon()
        if not icon.isNull():
            drag.setPixmap(icon.pixmap(120, 68))
        drag.exec(Qt.CopyAction)

    # --- incoming drop (OS files) ---------------------------------------

    def _urls_if_files(self, event) -> list[str] | None:  # noqa: ANN001
        md = event.mimeData()
        if not md.hasUrls():
            return None
        paths = [u.toLocalFile() for u in md.urls() if u.toLocalFile()]
        return paths or None

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if self._urls_if_files(event):
            event.acceptProposedAction()
            self._drop_highlight = True
            self.viewport().update()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:  # noqa: ANN001
        if self._urls_if_files(event):
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dragLeaveEvent(self, event) -> None:  # noqa: ANN001
        self._drop_highlight = False
        self.viewport().update()
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        self._drop_highlight = False
        self.viewport().update()
        paths = self._urls_if_files(event)
        if paths:
            event.acceptProposedAction()
            self.filesDropped.emit(paths)
            return
        super().dropEvent(event)

    # --- empty-state placeholder ----------------------------------------

    def paintEvent(self, event) -> None:  # noqa: ANN001
        super().paintEvent(event)
        if self.count() > 0 and not self._drop_highlight:
            return
        vp = self.viewport()
        p = QPainter(vp)
        p.setRenderHint(QPainter.Antialiasing, True)

        if self._drop_highlight:
            overlay = QColor(94, 234, 212, 28)
            p.fillRect(vp.rect(), overlay)

        if self.count() == 0:
            # Large icon + text; matches the VideoPad pattern.
            cx, cy = vp.width() // 2, vp.height() // 2
            icon_color = theme.C_BORDER_HI if not self._drop_highlight else theme.C_ACCENT
            pen = QPen(icon_color, 3)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)

            box_w, box_h = 76, 86
            box = QRect(cx - box_w // 2, cy - box_h // 2 - 14, box_w, box_h)
            p.drawRoundedRect(box.adjusted(0, 0, -1, -1), 8, 8)
            # little page-corner fold
            p.drawLine(box.right() - 22, box.top() + 4, box.right() - 4, box.top() + 22)
            # arrow pointing down
            arr_cx = box.center().x()
            arr_top = box.top() + 22
            arr_bot = box.bottom() - 12
            p.drawLine(arr_cx, arr_top, arr_cx, arr_bot - 10)
            head = QPolygonF([
                QPointF(arr_cx - 10, arr_bot - 12),
                QPointF(arr_cx + 10, arr_bot - 12),
                QPointF(arr_cx, arr_bot),
            ])
            p.setBrush(QBrush(icon_color))
            p.drawPolygon(head)

            p.setPen(theme.C_TEXT_2 if not self._drop_highlight else theme.C_ACCENT)
            f = p.font(); f.setPointSize(10); p.setFont(f)
            text_rect = QRect(0, cy + box_h // 2 - 10, vp.width(), 40)
            p.drawText(text_rect, Qt.AlignHCenter | Qt.AlignTop, self.EMPTY_HINT[0])
            p.setPen(theme.C_TEXT_3)
            p.drawText(
                QRect(0, cy + box_h // 2 + 10, vp.width(), 24),
                Qt.AlignHCenter | Qt.AlignTop,
                self.EMPTY_HINT[1],
            )
        p.end()


class ClipBin(QWidget):
    """Left-side library with Video Files / Audio Files / Images / Subs tabs.

    There's deliberately no header row — users import by dragging files
    onto a tab (or onto the main window). Each tab's empty state spells out
    the drop hint, so a dedicated "+ Video" button is redundant.
    """

    assetActivated = Signal(str)            # double-click
    assetDeleteRequested = Signal(str)      # Delete / Backspace on an asset
    subDeleteRequested = Signal(str)        # Delete / Backspace on a subtitle row
    filesDropped = Signal(list)             # list[str] - dropped from OS
    browseRequested = Signal(str)           # kind ("video"/"audio"/"image"/"sub")
    subActivated = Signal(str)              # subtitle id (double-click = make active)
    subStyleRequested = Signal()            # open style dialog
    subSyncRequested = Signal()             # open auto-sync / offset dialog

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setObjectName("CovePanel")
        self.setMinimumWidth(280)

        root = QVBoxLayout(self)
        root.setContentsMargins(1, 1, 1, 1)
        root.setSpacing(0)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setUsesScrollButtons(False)
        self.tabs.tabBar().setExpanding(False)
        self.tabs.setStyleSheet(
            "QTabWidget::pane {"
            f" border:none; background:{theme.PANEL};"
            " border-top: 1px solid " + theme.BORDER + ";"
            "}"
            "QTabBar { qproperty-drawBase:0; background: #0f161a;"
            " border-top-left-radius: 12px; border-top-right-radius: 12px; }"
            "QTabBar::tab {"
            f" background: transparent; color: {theme.TEXT_3};"
            " padding: 6px 8px; margin: 4px 2px; border-radius: 6px;"
            " font-size: 12px; font-weight: 500; min-width: 0;"
            "}"
            "QTabBar::tab:hover { color: " + theme.TEXT_2 + "; }"
            "QTabBar::tab:selected {"
            f" background: #1c272d; color: {theme.TEXT}; }}"
        )
        self.video_list = AssetList()
        self.audio_list = AssetList()
        self.image_list = AssetList()
        for lst, kind in (
            (self.video_list, "video"),
            (self.audio_list, "audio"),
            (self.image_list, "image"),
        ):
            lst.itemDoubleClicked.connect(self._on_activated)
            lst.deleteRequested.connect(self.assetDeleteRequested)
            lst.filesDropped.connect(self.filesDropped)
            lst.browseRequested.connect(lambda k=kind: self.browseRequested.emit(k))
        # Subs tab: a list of uploaded .srt/.vtt files with a small style
        # button up top. The AssetList for subs emits `deleteRequested` via
        # a dedicated signal so app.py can distinguish sub deletions from
        # media asset deletions.
        self.subs_list = AssetList()
        self.subs_list.itemDoubleClicked.connect(self._on_sub_activated)
        self.subs_list.deleteRequested.connect(self.subDeleteRequested)
        self.subs_list.filesDropped.connect(self.filesDropped)
        self.subs_list.browseRequested.connect(lambda: self.browseRequested.emit("sub"))
        self._sub_tab = self._build_subs_tab()

        # Keep references so we can relabel based on item counts. Short
        # labels match the design mockup and keep all four tabs visible at
        # 280 px clip-bin widths.
        self._tabs: list[tuple[str, QListWidget | QWidget]] = [
            ("Video", self.video_list),
            ("Audio", self.audio_list),
            ("Images", self.image_list),
            ("Subs", self._sub_tab),
        ]
        for base, widget in self._tabs:
            self.tabs.addTab(widget, base)
        root.addWidget(self.tabs, stretch=1)
        self._refresh_tab_labels()

    def _build_subs_tab(self) -> QWidget:
        container = QWidget()
        lay = QVBoxLayout(container)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        # Top strip with a Style… button.
        strip = QHBoxLayout()
        strip.setContentsMargins(6, 4, 6, 4)
        hint = QLabel(
            "Drop an SRT/VTT — double-click to set active for burn-in."
        )
        hint.setStyleSheet(f"color:{theme.TEXT_3}; font-size:11px;")
        hint.setWordWrap(True)
        strip.addWidget(hint, stretch=1)
        self.sync_btn = QPushButton("Sync…")
        self.sync_btn.setToolTip(
            "Nudge the active subtitle, or auto-sync it to the first clip's audio."
        )
        self.sync_btn.clicked.connect(self.subSyncRequested)
        strip.addWidget(self.sync_btn)
        self.style_btn = QPushButton("Style…")
        self.style_btn.clicked.connect(self.subStyleRequested)
        strip.addWidget(self.style_btn)
        lay.addLayout(strip)
        lay.addWidget(self.subs_list, stretch=1)
        return container

    # Drops on the panel outside the list (e.g. the header / tab bar)
    # still route in as imports.
    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        if event.mimeData().hasUrls():
            paths = [u.toLocalFile() for u in event.mimeData().urls() if u.toLocalFile()]
            if paths:
                event.acceptProposedAction()
                self.filesDropped.emit(paths)

    def _list_for_kind(self, kind: str) -> AssetList:
        if kind == "audio":
            return self.audio_list
        if kind == "image":
            return self.image_list
        return self.video_list

    def add_asset(self, asset: MediaAsset) -> None:
        lst = self._list_for_kind(asset.kind)
        item = QListWidgetItem(asset.path.name)
        item.setData(Qt.UserRole, asset.id)
        item.setToolTip(str(asset.path))
        if asset.thumb is not None and not asset.thumb.isNull():
            pm = QPixmap.fromImage(asset.thumb).scaled(
                120, 68, Qt.KeepAspectRatio, Qt.SmoothTransformation,
            )
            item.setIcon(QIcon(pm))
        else:
            item.setIcon(QIcon(_make_placeholder_pixmap(asset.kind)))
        lst.addItem(item)
        self.tabs.setCurrentWidget(lst)
        self._refresh_tab_labels()

    def remove_asset(self, asset_id: str) -> None:
        for lst in (self.video_list, self.audio_list, self.image_list):
            for i in range(lst.count() - 1, -1, -1):
                it = lst.item(i)
                if it and it.data(Qt.UserRole) == asset_id:
                    lst.takeItem(i)
            lst.viewport().update()
        self._refresh_tab_labels()

    def set_asset_thumb(self, asset_id: str, img) -> None:  # noqa: ANN001
        for lst in (self.video_list, self.audio_list, self.image_list):
            for i in range(lst.count()):
                it = lst.item(i)
                if it and it.data(Qt.UserRole) == asset_id:
                    pm = QPixmap.fromImage(img).scaled(
                        120, 68, Qt.KeepAspectRatio, Qt.SmoothTransformation,
                    )
                    it.setIcon(QIcon(pm))
                    return

    # --- Subs tab API ---------------------------------------------------

    def add_sub(self, sub_id: str, name: str, tooltip: str, active: bool) -> None:
        item = QListWidgetItem(name)
        item.setData(Qt.UserRole, sub_id)
        item.setToolTip(tooltip)
        item.setIcon(QIcon(_make_placeholder_pixmap("sub", active=active)))
        self.subs_list.addItem(item)
        self._refresh_tab_labels()

    def remove_sub(self, sub_id: str) -> None:
        for i in range(self.subs_list.count() - 1, -1, -1):
            it = self.subs_list.item(i)
            if it and it.data(Qt.UserRole) == sub_id:
                self.subs_list.takeItem(i)
        self.subs_list.viewport().update()
        self._refresh_tab_labels()

    def set_active_sub(self, active_sub_id: str) -> None:
        """Update every sub row to reflect which entry is the active one.

        Active state is communicated by the tile icon (green CC glyph) and
        tab selection; a prefix marker on the label would clutter the row.
        """
        for i in range(self.subs_list.count()):
            it = self.subs_list.item(i)
            if not it:
                continue
            sid = it.data(Qt.UserRole)
            is_active = sid == active_sub_id
            # Defensive cleanup for rows that may have been created by an
            # older version of the app that prepended a marker.
            raw = it.text().removeprefix("● ").removeprefix("○ ")
            it.setText(raw)
            it.setIcon(QIcon(_make_placeholder_pixmap("sub", active=is_active)))

    def _on_activated(self, item: QListWidgetItem) -> None:
        asset_id = item.data(Qt.UserRole)
        if asset_id:
            self.assetActivated.emit(asset_id)

    def _on_sub_activated(self, item: QListWidgetItem) -> None:
        sub_id = item.data(Qt.UserRole)
        if sub_id:
            self.subActivated.emit(sub_id)

    # --- tab labels -----------------------------------------------------

    def _refresh_tab_labels(self) -> None:
        counts = {
            "Video": self.video_list.count(),
            "Audio": self.audio_list.count(),
            "Images": self.image_list.count(),
            "Subs": self.subs_list.count(),
        }
        for idx, (base, _widget) in enumerate(self._tabs):
            n = counts.get(base, 0)
            label = f"{base} ({n})" if n > 0 else base
            self.tabs.setTabText(idx, label)


def _make_placeholder_pixmap(kind: str, *, active: bool = False) -> QPixmap:
    """Tile-sized pixmap shown before a real thumbnail is available, so the
    clip bin tiles never look like bare text."""
    pm = QPixmap(120, 68)
    pm.fill(QColor("#05090b"))
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    border = QPen(theme.C_BORDER, 1)
    p.setPen(border)
    p.drawRect(0, 0, 119, 67)
    p.setPen(Qt.NoPen)
    if kind == "video":
        icon_color = theme.C_ACCENT
    elif kind == "audio":
        icon_color = theme.C_WARN
    elif kind == "image":
        icon_color = QColor("#b67aff")
    else:  # sub
        icon_color = theme.C_OK if active else theme.C_TEXT_3
    p.setBrush(icon_color)
    if kind == "video":
        # Play triangle, centered.
        poly = QPolygonF([
            QPointF(50, 22),
            QPointF(50, 46),
            QPointF(74, 34),
        ])
        p.drawPolygon(poly)
    elif kind == "audio":
        # Simple speaker silhouette.
        p.drawRoundedRect(48, 24, 8, 20, 1.5, 1.5)
        horn = QPolygonF([
            QPointF(56, 24),
            QPointF(70, 16),
            QPointF(70, 52),
            QPointF(56, 44),
        ])
        p.drawPolygon(horn)
    elif kind == "image":
        # Mountain-and-sun silhouette.
        p.drawRect(40, 22, 40, 26)
        p.setBrush(QColor("#1d2026"))
        p.drawEllipse(68, 26, 7, 7)
        p.setBrush(icon_color)
        mountains = QPolygonF([
            QPointF(40, 48),
            QPointF(54, 30),
            QPointF(62, 40),
            QPointF(72, 28),
            QPointF(80, 48),
        ])
        p.drawPolygon(mountains)
    else:  # sub
        # "CC"-style caption tag.
        p.setPen(QPen(icon_color, 2))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(QRect(36, 22, 48, 26), 5, 5)
        p.setPen(QPen(icon_color, 2))
        p.drawLine(46, 35, 54, 35)
        p.drawLine(66, 35, 74, 35)
    p.end()
    return pm
