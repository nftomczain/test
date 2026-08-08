"""GUI GhostPoster (v0.3) oparty o PySide6, z przełącznikiem języka PL/EN.

Uruchomienie: `ghostposter-gui` (po `pip install ghostposter[gui]`)
albo `python -m ghostposter.gui`.

Zaprojektowane pod obsługę jedną ręką i z klawiatury: każda akcja dostępna
też bez przeciągania (przycisk "Wybierz plik...", skróty klawiszowe,
strefa upuszczania aktywna też przez Enter/Spację po najechaniu Tabem),
oraz pod czytniki ekranu: wszystkie kontrolki mają ustawione accessibleName.

Zawiera podgląd strony źródłowej z nałożoną siatką podziału — aktualizuje
się automatycznie po zmianie formatu papieru, zakładki, numeru strony,
Auto Crop czy jego marginesu.

Interfejs jest dwujęzyczny (patrz `i18n.py`) — wybór języka jest
zapamiętywany między uruchomieniami razem z resztą ustawień.
"""

from __future__ import annotations

import sys
from datetime import date
from html import escape as html_escape
from pathlib import Path

import fitz  # PyMuPDF
from PySide6.QtCore import QRectF, QSettings, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QDesktopServices,
    QFont,
    QImage,
    QKeySequence,
    QPainter,
    QPen,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplashScreen,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import __version__
from .blank import find_blank_tiles
from .crop import detect_content_bbox
from .geometry import Tile, compute_best_grid, compute_grid, translate_tiles
from .i18n import DEFAULT_LANG, t
from .paper import available_sizes, get_paper_size_pt
from .tiler import PageNumberOutOfRangeError, get_page_size_pt
from .utils import mm_to_pt, pt_to_mm
from .writer import write_tiled_pdf

PROJECT_URL = "https://github.com/nftomczain/GhostPoster"
_ASSETS_DIR = Path(__file__).resolve().parent / "assets"
SPLASH_IMAGE_PATH = _ASSETS_DIR / "splash.png"
SPLASH_DURATION_MS = 1500

PREVIEW_RENDER_MAX_PX = 900  # rozdzielczość renderowanej strony (niezależna od rozmiaru okna)
TILE_COLORS = [
    QColor(47, 111, 235, 200),
    QColor(219, 68, 55, 200),
    QColor(15, 157, 88, 200),
    QColor(244, 160, 0, 200),
]
CROP_MARGIN_OPTIONS_MM = [0, 2, 5, 10, 20]
DEFAULT_CROP_MARGIN_MM = 5


class DropZone(QFrame):
    """Strefa upuszczania PDF — obsługuje też klik i Enter/Spację (bez myszy)."""

    file_chosen = Signal(Path)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.lang = DEFAULT_LANG
        self.setAcceptDrops(True)
        self.setFrameShape(QFrame.StyledPanel)
        self.setMinimumHeight(80)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setStyleSheet(
            "DropZone { border: 2px dashed #888; border-radius: 8px; }"
            "DropZone:focus { border-color: #2f6feb; }"
        )

        layout = QVBoxLayout(self)
        self._label = QLabel(self)
        self._label.setAlignment(Qt.AlignCenter)
        font = self._label.font()
        font.setPointSize(font.pointSize() + 2)
        self._label.setFont(font)
        layout.addWidget(self._label)
        self._chosen_path: Path | None = None
        self.retranslate(self.lang)

    def retranslate(self, lang: str) -> None:
        self.lang = lang
        self.setAccessibleName(t(lang, "drop_zone_accessible_name"))
        self.setAccessibleDescription(t(lang, "drop_zone_accessible_desc"))
        if self._chosen_path is not None:
            self._label.setText(t(lang, "drop_zone_chosen", name=self._chosen_path.name))
        else:
            self._label.setText(t(lang, "drop_zone_text"))

    def dragEnterEvent(self, event) -> None:  # noqa: ANN001
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: ANN001
        urls = event.mimeData().urls()
        if urls:
            path = Path(urls[0].toLocalFile())
            if path.suffix.lower() == ".pdf":
                self.set_file(path)
            else:
                QMessageBox.warning(
                    self, t(self.lang, "bad_file_title"), t(self.lang, "bad_file_body")
                )

    def mousePressEvent(self, event) -> None:  # noqa: ANN001
        self._browse()

    def keyPressEvent(self, event) -> None:  # noqa: ANN001
        if event.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self._browse()
        else:
            super().keyPressEvent(event)

    def _browse(self) -> None:
        path_str, _ = QFileDialog.getOpenFileName(
            self, t(self.lang, "choose_pdf_dialog"), "", "PDF (*.pdf)"
        )
        if path_str:
            self.set_file(Path(path_str))

    def set_file(self, path: Path) -> None:
        self._chosen_path = path
        self._label.setText(t(self.lang, "drop_zone_chosen", name=path.name))
        self.file_chosen.emit(path)


class PreviewWidget(QWidget):
    """Podgląd strony źródłowej z nałożoną siatką podziału na arkusze.

    Renderowanie strony (kosztowne) i przeliczanie siatki (tanie) są
    rozdzielone: `set_source_page` robi jedno i drugie, `set_tiles`
    tylko odświeża nałożoną siatkę bez ponownego renderowania PDF.

    Obsługuje też tryb ręcznego wyboru obszaru treści (Manual Crop):
    po włączeniu `enable_crop_edit_mode()` można przeciągać myszką
    narożniki prostokąta wprost na podglądzie, jak w CAD-zie.
    """

    crop_rect_changed = Signal()
    crop_rect_live = Signal()  # emitowany podczas przeciągania (na żywo), nie tylko na koniec
    crop_double_clicked = Signal()
    file_dropped = Signal(Path)
    HANDLE_PX = 9
    ZOOM_MIN = 1.0
    ZOOM_MAX = 2.0  # 200%
    ZOOM_STEP = 1.15

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.lang = DEFAULT_LANG
        self.setMinimumSize(320, 320)
        self.setMouseTracking(True)
        self.setAcceptDrops(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self._base_pixmap: QPixmap | None = None
        self._page_size_pt: tuple[float, float] | None = None
        self._tiles: list[Tile] = []
        self._crop_edit_mode = False
        self._crop_rect_pt: fitz.Rect | None = None
        self._drag_handle: str | None = None
        self._drag_anchor_pt: tuple[float, float] | None = None
        self._zoom = 1.0  # 1.0 = dopasuj do okna, do ZOOM_MAX (200%)
        self._pan_x = 0.0
        self._pan_y = 0.0
        self._panning = False
        self._pan_last_pos: tuple[float, float] | None = None
        self.retranslate(self.lang)

    def retranslate(self, lang: str) -> None:
        self.lang = lang
        self.setAccessibleName(t(lang, "preview_accessible_name"))
        self.update()

    def clear(self) -> None:
        self._base_pixmap = None
        self._page_size_pt = None
        self._tiles = []
        self._crop_rect_pt = None
        self._crop_edit_mode = False
        self._zoom = 1.0
        self._pan_x = 0.0
        self._pan_y = 0.0
        self.update()

    def set_source_page(self, input_path: Path, page_number: int) -> tuple[float, float]:
        """Renderuje wskazaną stronę PDF i zwraca jej rozmiar w punktach."""
        with fitz.open(input_path) as doc:
            page = doc[page_number]
            rect = page.rect
            scale = PREVIEW_RENDER_MAX_PX / max(rect.width, rect.height)
            pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale))
            img = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format_RGB888)
            self._base_pixmap = QPixmap.fromImage(img.copy())
            self._page_size_pt = (rect.width, rect.height)
        self._tiles = []
        self._crop_rect_pt = None
        self._crop_edit_mode = False
        self._zoom = 1.0
        self._pan_x = 0.0
        self._pan_y = 0.0
        self.update()
        return self._page_size_pt

    def set_tiles(self, tiles: list[Tile]) -> None:
        self._tiles = tiles
        self.update()

    # -- tryb reczengo wyboru obszaru (Manual Crop) -----------------------

    def enable_crop_edit_mode(self, initial_rect_pt: fitz.Rect | None) -> None:
        """Włącza tryb edycji: użytkownik przeciąga narożniki na podglądzie."""
        self._crop_edit_mode = True
        if initial_rect_pt is not None:
            self._crop_rect_pt = fitz.Rect(initial_rect_pt)
        elif self._page_size_pt is not None:
            pw, ph = self._page_size_pt
            margin_w, margin_h = pw * 0.1, ph * 0.1
            self._crop_rect_pt = fitz.Rect(margin_w, margin_h, pw - margin_w, ph - margin_h)
        self.update()

    def disable_crop_edit_mode(self) -> None:
        self._crop_edit_mode = False
        self._drag_handle = None
        self.update()

    def set_crop_rect_pt(self, rect: fitz.Rect | None) -> None:
        """Ustawia prostokąt programowo (np. z pól liczbowych X0/Y0/X1/Y1)."""
        self._crop_rect_pt = fitz.Rect(rect) if rect is not None else None
        self.update()

    @property
    def crop_rect_pt(self) -> fitz.Rect | None:
        return self._crop_rect_pt

    def _transform(self) -> tuple[float, float, float] | None:
        """(scale, offset_x, offset_y) — mapowanie punktów strony na piksele widgetu."""
        if self._page_size_pt is None:
            return None
        page_w_pt, page_h_pt = self._page_size_pt
        widget_w, widget_h = self.width(), self.height()
        fit_scale = min(widget_w / page_w_pt, widget_h / page_h_pt)
        scale = fit_scale * self._zoom
        draw_w, draw_h = page_w_pt * scale, page_h_pt * scale
        base_ox, base_oy = (widget_w - draw_w) / 2, (widget_h - draw_h) / 2
        return scale, base_ox + self._pan_x, base_oy + self._pan_y

    def _clamp_pan(self, pan_x: float, pan_y: float, scale: float) -> tuple[float, float]:
        """Nie pozwala odsunąć przybliżonej strony całkowicie poza widget —
        przynajmniej jedna krawędź obrazu zawsze zachodzi na widoczny obszar."""
        if self._page_size_pt is None:
            return 0.0, 0.0
        pw, ph = self._page_size_pt
        widget_w, widget_h = self.width(), self.height()
        draw_w, draw_h = pw * scale, ph * scale
        base_ox, base_oy = (widget_w - draw_w) / 2, (widget_h - draw_h) / 2

        if draw_w > widget_w:
            min_ox, max_ox = widget_w - draw_w, 0.0
        else:
            min_ox = max_ox = base_ox
        if draw_h > widget_h:
            min_oy, max_oy = widget_h - draw_h, 0.0
        else:
            min_oy = max_oy = base_oy

        offset_x = min(max(base_ox + pan_x, min_ox), max_ox)
        offset_y = min(max(base_oy + pan_y, min_oy), max_oy)
        return offset_x - base_ox, offset_y - base_oy

    def _page_to_widget(self, x_pt: float, y_pt: float) -> tuple[float, float]:
        scale, ox, oy = self._transform()
        return ox + x_pt * scale, oy + y_pt * scale

    def _widget_to_page(self, x_px: float, y_px: float) -> tuple[float, float]:
        scale, ox, oy = self._transform()
        pw, ph = self._page_size_pt
        x_pt = min(max((x_px - ox) / scale, 0.0), pw)
        y_pt = min(max((y_px - oy) / scale, 0.0), ph)
        return x_pt, y_pt

    def wheelEvent(self, event) -> None:  # noqa: ANN001
        if self._page_size_pt is None or self._crop_edit_mode:
            super().wheelEvent(event)
            return
        delta = event.angleDelta().y()
        if delta == 0:
            return

        old_scale, old_ox, old_oy = self._transform()
        pos = event.position() if hasattr(event, "position") else event.pos()
        cursor_x, cursor_y = pos.x(), pos.y()
        # punkt strony pod kursorem PRZED zmianą zoomu - ma zostac pod kursorem po niej
        page_x = (cursor_x - old_ox) / old_scale
        page_y = (cursor_y - old_oy) / old_scale

        factor = self.ZOOM_STEP if delta > 0 else 1 / self.ZOOM_STEP
        new_zoom = min(max(self._zoom * factor, self.ZOOM_MIN), self.ZOOM_MAX)
        if new_zoom == self._zoom:
            event.accept()
            return
        self._zoom = new_zoom

        pw, ph = self._page_size_pt
        widget_w, widget_h = self.width(), self.height()
        fit_scale = min(widget_w / pw, widget_h / ph)
        new_scale = fit_scale * self._zoom
        new_draw_w, new_draw_h = pw * new_scale, ph * new_scale
        base_ox, base_oy = (widget_w - new_draw_w) / 2, (widget_h - new_draw_h) / 2
        desired_pan_x = (cursor_x - page_x * new_scale) - base_ox
        desired_pan_y = (cursor_y - page_y * new_scale) - base_oy
        self._pan_x, self._pan_y = self._clamp_pan(desired_pan_x, desired_pan_y, new_scale)

        self.update()
        event.accept()

    def _handle_at(self, pos_x: float, pos_y: float) -> str | None:
        """Zwraca nazwę uchwytu narożnika pod kursorem, albo 'move' jeśli
        kursor jest wewnątrz prostokąta, albo None."""
        if self._crop_rect_pt is None:
            return None
        r = self._crop_rect_pt
        corners = {
            "tl": (r.x0, r.y0),
            "tr": (r.x1, r.y0),
            "bl": (r.x0, r.y1),
            "br": (r.x1, r.y1),
        }
        for name, (cx, cy) in corners.items():
            wx, wy = self._page_to_widget(cx, cy)
            if abs(wx - pos_x) <= self.HANDLE_PX and abs(wy - pos_y) <= self.HANDLE_PX:
                return name
        x_pt, y_pt = self._widget_to_page(pos_x, pos_y)
        if r.x0 <= x_pt <= r.x1 and r.y0 <= y_pt <= r.y1:
            return "move"
        return None

    def mousePressEvent(self, event) -> None:  # noqa: ANN001
        if self._crop_edit_mode:
            if self._page_size_pt is None:
                return
            pos = event.position() if hasattr(event, "position") else event.pos()
            px, py = pos.x(), pos.y()
            handle = self._handle_at(px, py)
            if handle is None:
                # klik poza prostokatem -> zaczynamy rysowac nowy od tego rogu
                x_pt, y_pt = self._widget_to_page(px, py)
                self._crop_rect_pt = fitz.Rect(x_pt, y_pt, x_pt, y_pt)
                handle = "br"
            self._drag_handle = handle
            self._drag_anchor_pt = self._widget_to_page(px, py)
            self.update()
        elif self._page_size_pt is None:
            # brak wczytanego pliku - klik na pustym podgladzie tez otwiera wybor pliku
            self._browse()
        elif self._zoom > self.ZOOM_MIN:
            pos = event.position() if hasattr(event, "position") else event.pos()
            self._panning = True
            self._pan_last_pos = (pos.x(), pos.y())
            self.setCursor(Qt.ClosedHandCursor)

    def keyPressEvent(self, event) -> None:  # noqa: ANN001
        if (
            not self._crop_edit_mode
            and self._page_size_pt is None
            and event.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space)
        ):
            self._browse()
        else:
            super().keyPressEvent(event)

    def dragEnterEvent(self, event) -> None:  # noqa: ANN001
        if not self._crop_edit_mode and event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: ANN001
        if self._crop_edit_mode:
            return
        urls = event.mimeData().urls()
        if urls:
            path = Path(urls[0].toLocalFile())
            if path.suffix.lower() == ".pdf":
                self.file_dropped.emit(path)
            else:
                QMessageBox.warning(
                    self, t(self.lang, "bad_file_title"), t(self.lang, "bad_file_body")
                )

    def _browse(self) -> None:
        path_str, _ = QFileDialog.getOpenFileName(
            self, t(self.lang, "choose_pdf_dialog"), "", "PDF (*.pdf)"
        )
        if path_str:
            self.file_dropped.emit(Path(path_str))

    def mouseMoveEvent(self, event) -> None:  # noqa: ANN001
        if self._panning and self._pan_last_pos is not None:
            pos = event.position() if hasattr(event, "position") else event.pos()
            transform = self._transform()
            if transform is None:
                return
            scale, _, _ = transform
            dx = pos.x() - self._pan_last_pos[0]
            dy = pos.y() - self._pan_last_pos[1]
            self._pan_x, self._pan_y = self._clamp_pan(self._pan_x + dx, self._pan_y + dy, scale)
            self._pan_last_pos = (pos.x(), pos.y())
            self.update()
            return

        if not self._crop_edit_mode or self._drag_handle is None or self._crop_rect_pt is None:
            return
        pos = event.position() if hasattr(event, "position") else event.pos()
        x_pt, y_pt = self._widget_to_page(pos.x(), pos.y())
        r = self._crop_rect_pt

        if self._drag_handle == "move":
            ax, ay = self._drag_anchor_pt
            dx, dy = x_pt - ax, y_pt - ay
            pw, ph = self._page_size_pt
            new_x0 = min(max(r.x0 + dx, 0.0), pw - r.width)
            new_y0 = min(max(r.y0 + dy, 0.0), ph - r.height)
            self._crop_rect_pt = fitz.Rect(new_x0, new_y0, new_x0 + r.width, new_y0 + r.height)
            self._drag_anchor_pt = (x_pt, y_pt)
        else:
            x0, y0, x1, y1 = r.x0, r.y0, r.x1, r.y1
            if self._drag_handle in ("tl", "bl"):
                x0 = x_pt
            if self._drag_handle in ("tr", "br"):
                x1 = x_pt
            if self._drag_handle in ("tl", "tr"):
                y0 = y_pt
            if self._drag_handle in ("bl", "br"):
                y1 = y_pt
            self._crop_rect_pt = fitz.Rect(x0, y0, x1, y1)

        self.update()
        self.crop_rect_live.emit()

    def mouseReleaseEvent(self, event) -> None:  # noqa: ANN001
        if self._panning:
            self._panning = False
            self._pan_last_pos = None
            self.setCursor(Qt.ArrowCursor)
            return

        if not self._crop_edit_mode or self._drag_handle is None:
            return
        if self._crop_rect_pt is not None:
            self._crop_rect_pt.normalize()
            if self._crop_rect_pt.width < 5 or self._crop_rect_pt.height < 5:
                # zbyt maly prostokat (przypadkowy klik) - ignorujemy
                self._crop_rect_pt = None
        self._drag_handle = None
        self.update()
        self.crop_rect_changed.emit()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: ANN001
        if self._crop_edit_mode and self._crop_rect_pt is not None:
            self.crop_double_clicked.emit()

    def paintEvent(self, event) -> None:  # noqa: ANN001
        painter = QPainter(self)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        painter.fillRect(self.rect(), QColor("#f2f2f2"))

        if self._base_pixmap is None or self._page_size_pt is None:
            painter.setPen(QColor("#666"))
            painter.setFont(QFont(painter.font().family(), 10))
            painter.drawText(self.rect(), Qt.AlignCenter, t(self.lang, "preview_placeholder"))
            painter.end()
            return

        page_w_pt, page_h_pt = self._page_size_pt
        scale, offset_x, offset_y = self._transform()
        draw_w, draw_h = page_w_pt * scale, page_h_pt * scale

        target = QRectF(offset_x, offset_y, draw_w, draw_h)
        painter.drawPixmap(target, self._base_pixmap, QRectF(self._base_pixmap.rect()))

        for tile in self._tiles:
            color = TILE_COLORS[(tile.row + tile.col) % len(TILE_COLORS)]
            pen = QPen(color, 2)
            painter.setPen(pen)
            rect = QRectF(
                offset_x + tile.x0 * scale,
                offset_y + tile.y0 * scale,
                tile.width * scale,
                tile.height * scale,
            )
            painter.drawRect(rect)
            painter.setPen(QColor(color.red(), color.green(), color.blue(), 255))
            painter.drawText(rect.adjusted(3, 2, 0, 0), Qt.AlignLeft | Qt.AlignTop, tile.label)

        if self._crop_edit_mode and self._crop_rect_pt is not None:
            r = self._crop_rect_pt
            x0, y0 = self._page_to_widget(r.x0, r.y0)
            x1, y1 = self._page_to_widget(r.x1, r.y1)
            crop_qrect = QRectF(x0, y0, x1 - x0, y1 - y0)

            painter.fillRect(self.rect(), QColor(0, 0, 0, 90))
            painter.drawPixmap(crop_qrect, self._base_pixmap, self._page_rect_to_pixmap_rect(r))

            pen = QPen(QColor("#2f6feb"), 2, Qt.DashLine)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(crop_qrect)

            painter.setPen(QPen(QColor("#2f6feb"), 1))
            painter.setBrush(QColor("#2f6feb"))
            h = self.HANDLE_PX
            for cx, cy in ((x0, y0), (x1, y0), (x0, y1), (x1, y1)):
                painter.drawRect(QRectF(cx - h / 2, cy - h / 2, h, h))

        if self._zoom > self.ZOOM_MIN:
            label = f"{round(self._zoom * 100)}%"
            painter.setFont(QFont(painter.font().family(), 9, QFont.Bold))
            metrics_rect = painter.fontMetrics().boundingRect(label)
            pad = 5
            badge = QRectF(
                self.width() - metrics_rect.width() - 2 * pad - 8,
                8,
                metrics_rect.width() + 2 * pad,
                metrics_rect.height() + 2 * pad,
            )
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(0, 0, 0, 140))
            painter.drawRoundedRect(badge, 4, 4)
            painter.setPen(QColor("white"))
            painter.drawText(badge, Qt.AlignCenter, label)

        painter.end()

    def _page_rect_to_pixmap_rect(self, r: fitz.Rect) -> QRectF:
        pw, ph = self._page_size_pt
        pix_w, pix_h = self._base_pixmap.width(), self._base_pixmap.height()
        return QRectF(
            r.x0 / pw * pix_w,
            r.y0 / ph * pix_h,
            (r.x1 - r.x0) / pw * pix_w,
            (r.y1 - r.y0) / ph * pix_h,
        )


class TileWorker(QThread):
    """Wykonuje planowanie i zapis w osobnym wątku, żeby nie blokować GUI.

    Emituje wyniki jako dane strukturalne (liczby, ścieżki), nie gotowe
    zdania — tekst do wyświetlenia składa MainWindow przez `i18n.t()`,
    dzięki czemu worker nie musi nic wiedzieć o aktualnym języku GUI.
    """

    finished_ok = Signal(int, int, str)  # (zapisane, pominięte_puste, output_path)
    failed = Signal(str)
    progress = Signal(int, int)  # (gotowe, razem)

    def __init__(
        self,
        input_path: Path,
        output_path: Path,
        paper: str,
        overlap_mm: float,
        page_number: int,
        marks: bool,
        cutlines: bool,
        labels: bool,
        maximize: bool = False,
        print_shop: bool = False,
        skip_blank: bool = False,
        auto_crop: bool = False,
        crop_margin_mm: float = DEFAULT_CROP_MARGIN_MM,
        blank_threshold_pct: float = 0.5,
        manual_crop_rect_pt: tuple[float, float, float, float] | None = None,
        label_style: str = "column",
    ) -> None:
        super().__init__()
        self.input_path = input_path
        self.output_path = output_path
        self.paper = paper
        self.overlap_mm = overlap_mm
        self.page_number = page_number
        self.marks = marks
        self.cutlines = cutlines
        self.labels = labels
        self.maximize = maximize
        self.print_shop = print_shop
        self.skip_blank = skip_blank
        self.auto_crop = auto_crop
        self.crop_margin_mm = crop_margin_mm
        self.blank_threshold_pct = blank_threshold_pct
        self.manual_crop_rect_pt = manual_crop_rect_pt
        self.label_style = label_style

    def run(self) -> None:
        try:
            paper_w_pt, paper_h_pt = get_paper_size_pt(self.paper)
            page_w_pt, page_h_pt = get_page_size_pt(self.input_path, self.page_number)
            overlap_pt = mm_to_pt(self.overlap_mm)

            effective_w_pt, effective_h_pt = page_w_pt, page_h_pt
            offset_x, offset_y = 0.0, 0.0
            if self.manual_crop_rect_pt is not None:
                rx0, ry0, rx1, ry1 = self.manual_crop_rect_pt
                effective_w_pt, effective_h_pt = rx1 - rx0, ry1 - ry0
                offset_x, offset_y = rx0, ry0
            elif self.auto_crop:
                bbox = detect_content_bbox(
                    self.input_path, self.page_number, padding_mm=self.crop_margin_mm
                )
                effective_w_pt, effective_h_pt = bbox.width, bbox.height
                offset_x, offset_y = bbox.x0, bbox.y0

            if self.maximize:
                result = compute_best_grid(
                    effective_w_pt,
                    effective_h_pt,
                    paper_w_pt,
                    paper_h_pt,
                    overlap_pt,
                    self.label_style,
                )
                tiles = result.tiles
                paper_w_pt, paper_h_pt = result.paper_width_pt, result.paper_height_pt
            else:
                tiles = compute_grid(
                    effective_w_pt,
                    effective_h_pt,
                    paper_w_pt,
                    paper_h_pt,
                    overlap_pt,
                    self.label_style,
                )

            if offset_x or offset_y:
                tiles = translate_tiles(tiles, offset_x, offset_y)

            skip_labels: set[str] = set()
            if self.skip_blank:
                blanks = find_blank_tiles(
                    self.input_path,
                    tiles,
                    self.page_number,
                    ink_threshold=self.blank_threshold_pct / 100,
                )
                skip_labels = {t.label for t in blanks}

            print_shop_info = None
            if self.print_shop:
                print_shop_info = {
                    "Plik źródłowy": self.input_path.name,
                    "Strona źródłowa": str(self.page_number),
                    "Format arkusza": self.paper,
                    "Orientacja": "poziomo" if paper_w_pt > paper_h_pt else "pionowo",
                    "Zakładka": f"{self.overlap_mm:.1f} mm",
                    "Liczba arkuszy": str(len(tiles) - len(skip_labels)),
                    "Rozmiar arkusza": (
                        f"{pt_to_mm(paper_w_pt):.0f} x {pt_to_mm(paper_h_pt):.0f} mm"
                    ),
                    "Skala": "100% (bez przeskalowania)",
                    "Data wygenerowania": date.today().isoformat(),
                }

            write_tiled_pdf(
                input_path=self.input_path,
                output_path=self.output_path,
                tiles=tiles,
                paper_width_pt=paper_w_pt,
                paper_height_pt=paper_h_pt,
                page_number=self.page_number,
                draw_marks=self.marks,
                draw_cutlines=self.cutlines,
                draw_labels=self.labels,
                print_shop=self.print_shop,
                print_shop_info=print_shop_info,
                overlap_pt=overlap_pt,
                skip_labels=skip_labels,
                progress_callback=lambda done, total: self.progress.emit(done, total),
            )
            written = len(tiles) - len(skip_labels)
            self.finished_ok.emit(written, len(skip_labels), str(self.output_path))
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.lang = DEFAULT_LANG
        self.setWindowTitle(t(self.lang, "window_title"))
        self.resize(560, 730)
        # Okno ma sporo kontrolek pod podglądem — na mniejszych ekranach
        # (typowo 1366x768 na laptopach, minus pasek zadań) całość może nie
        # zmieścić się w pionie. Bez tego przycisk Eksportuj i część
        # checkboxów były wtedy całkowicie nieosiągalne, bez żadnego
        # sposobu, żeby się do nich dostać — stąd cała zawartość okna jest
        # owinięta w przewijany obszar zamiast być bezpośrednio centralnym
        # widgetem.
        self.setMinimumSize(420, 360)
        self._input_path: Path | None = None
        self._page_size_pt: tuple[float, float] | None = None
        self._content_bbox_pt: fitz.Rect | None = None
        self._manual_crop_rect_pt: fitz.Rect | None = None
        self._worker: TileWorker | None = None
        self._overlap_warning_active = False
        self._flash_count = 0
        self._flash_timer = QTimer(self)
        self._flash_timer.setInterval(220)
        self._flash_timer.timeout.connect(self._on_status_flash_tick)

        central = QWidget(self)
        root = QVBoxLayout(central)

        scroll_area = QScrollArea(self)
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QFrame.NoFrame)
        scroll_area.setWidget(central)
        self.setCentralWidget(scroll_area)

        lang_row = QHBoxLayout()
        lang_row.addStretch(1)
        self.help_button = QToolButton(self)
        self.help_button.setPopupMode(QToolButton.InstantPopup)
        self.help_button.setAutoRaise(True)
        self.help_button.setAccessibleName("Help")
        self.help_menu = QMenu(self.help_button)
        self.action_help_readme = QAction(self)
        self.action_help_readme.triggered.connect(
            lambda: QDesktopServices.openUrl(QUrl(PROJECT_URL))
        )
        self.action_help_guide = QAction(self)
        self.action_help_guide.triggered.connect(self._show_quick_guide)
        self.action_help_github = QAction(self)
        self.action_help_github.triggered.connect(
            lambda: QDesktopServices.openUrl(QUrl(PROJECT_URL))
        )
        self.action_help_report_bug = QAction(self)
        self.action_help_report_bug.triggered.connect(self._show_report_bug_dialog)
        self.action_help_about = QAction(self)
        self.action_help_about.triggered.connect(self._show_about_dialog)
        for action in (
            self.action_help_readme,
            self.action_help_guide,
            self.action_help_github,
            self.action_help_report_bug,
            self.action_help_about,
        ):
            self.help_menu.addAction(action)
        self.help_button.setMenu(self.help_menu)
        lang_row.addWidget(self.help_button)
        self.language_label = QLabel(self)
        lang_row.addWidget(self.language_label)
        self.language_combo = QComboBox(self)
        self.language_combo.addItem("Polski", userData="pl")
        self.language_combo.addItem("English", userData="en")
        self.language_combo.currentIndexChanged.connect(self._on_language_changed)
        lang_row.addWidget(self.language_combo)
        root.addLayout(lang_row)

        self.drop_zone = DropZone(self)
        self.drop_zone.file_chosen.connect(self._on_file_chosen)
        root.addWidget(self.drop_zone)

        self.manual_crop_hint_label = QLabel(self)
        self.manual_crop_hint_label.setAlignment(Qt.AlignCenter)
        self.manual_crop_hint_label.setStyleSheet(
            "QLabel { color: #2f6feb; font-weight: bold; padding: 2px; }"
        )
        self.manual_crop_hint_label.setVisible(False)
        root.addWidget(self.manual_crop_hint_label)

        self.preview = PreviewWidget(self)
        self.preview.file_dropped.connect(self._on_file_chosen)
        root.addWidget(self.preview, stretch=1)

        self.manual_crop_size_label = QLabel(self)
        self.manual_crop_size_label.setAlignment(Qt.AlignCenter)
        self.manual_crop_size_label.setVisible(False)
        root.addWidget(self.manual_crop_size_label)

        form = QFormLayout()

        self.paper_combo = QComboBox(self)
        self.paper_combo.addItems(available_sizes())
        self.paper_combo.setCurrentText("A3")
        self.paper_combo.currentTextChanged.connect(self._recompute_tiles)
        self.paper_label = QLabel(self)
        form.addRow(self.paper_label, self.paper_combo)

        self.overlap_spin = QDoubleSpinBox(self)
        self.overlap_spin.setRange(0, 50)
        self.overlap_spin.setValue(10)
        self.overlap_spin.setSuffix(" mm")
        self.overlap_spin.valueChanged.connect(self._recompute_tiles)
        self.overlap_label = QLabel(self)
        form.addRow(self.overlap_label, self.overlap_spin)

        self.page_spin = QSpinBox(self)
        self.page_spin.setRange(0, 9999)
        self.page_spin.valueChanged.connect(self._on_page_changed)
        self.page_label = QLabel(self)
        form.addRow(self.page_label, self.page_spin)

        self.crop_margin_combo = QComboBox(self)
        for mm in CROP_MARGIN_OPTIONS_MM:
            self.crop_margin_combo.addItem(f"{mm} mm", userData=mm)
        self.crop_margin_combo.setCurrentIndex(CROP_MARGIN_OPTIONS_MM.index(DEFAULT_CROP_MARGIN_MM))
        self.crop_margin_combo.currentIndexChanged.connect(self._on_crop_margin_changed)
        self.crop_margin_label = QLabel(self)
        form.addRow(self.crop_margin_label, self.crop_margin_combo)

        root.addLayout(form)

        checks = QHBoxLayout()
        self.marks_check = QCheckBox(self)
        self.cutlines_check = QCheckBox(self)
        self.labels_check = QCheckBox(self)
        self.maximize_check = QCheckBox(self)
        self.auto_crop_check = QCheckBox(self)
        self.letter_per_row_check = QCheckBox(self)
        for cb in (
            self.marks_check,
            self.cutlines_check,
            self.labels_check,
            self.maximize_check,
            self.auto_crop_check,
            self.letter_per_row_check,
        ):
            checks.addWidget(cb)
        self.maximize_check.toggled.connect(self._recompute_tiles)
        self.marks_check.toggled.connect(self._recompute_tiles)
        self.cutlines_check.toggled.connect(self._recompute_tiles)
        self.auto_crop_check.toggled.connect(self._on_auto_crop_toggled)
        self.letter_per_row_check.toggled.connect(self._recompute_tiles)
        root.addLayout(checks)

        manual_crop_row = QHBoxLayout()
        self.manual_crop_button = QPushButton(self)
        self.manual_crop_button.clicked.connect(self._on_manual_crop_button)
        manual_crop_row.addWidget(self.manual_crop_button)
        self.manual_crop_reset_button = QPushButton(self)
        self.manual_crop_reset_button.clicked.connect(self._on_manual_crop_reset)
        self.manual_crop_reset_button.setEnabled(False)
        manual_crop_row.addWidget(self.manual_crop_reset_button)

        self.manual_crop_x0_label = QLabel(self)
        manual_crop_row.addWidget(self.manual_crop_x0_label)
        self.manual_crop_x0_spin = self._make_crop_coord_spin()
        manual_crop_row.addWidget(self.manual_crop_x0_spin)
        self.manual_crop_y0_label = QLabel(self)
        manual_crop_row.addWidget(self.manual_crop_y0_label)
        self.manual_crop_y0_spin = self._make_crop_coord_spin()
        manual_crop_row.addWidget(self.manual_crop_y0_spin)
        self.manual_crop_x1_label = QLabel(self)
        manual_crop_row.addWidget(self.manual_crop_x1_label)
        self.manual_crop_x1_spin = self._make_crop_coord_spin()
        manual_crop_row.addWidget(self.manual_crop_x1_spin)
        self.manual_crop_y1_label = QLabel(self)
        manual_crop_row.addWidget(self.manual_crop_y1_label)
        self.manual_crop_y1_spin = self._make_crop_coord_spin()
        manual_crop_row.addWidget(self.manual_crop_y1_spin)
        manual_crop_row.addStretch(1)
        root.addLayout(manual_crop_row)
        for spin in (
            self.manual_crop_x0_spin,
            self.manual_crop_y0_spin,
            self.manual_crop_x1_spin,
            self.manual_crop_y1_spin,
        ):
            spin.setEnabled(False)
            spin.valueChanged.connect(self._on_manual_crop_spin_changed)
        self.preview.crop_rect_changed.connect(self._on_preview_crop_rect_changed)
        self.preview.crop_rect_live.connect(self._on_preview_crop_rect_live)
        self.preview.crop_double_clicked.connect(self._on_manual_crop_button)

        print_shop_row = QHBoxLayout()
        self.print_shop_check = QCheckBox(self)
        self.print_shop_check.setStyleSheet("QCheckBox { font-weight: bold; }")
        print_shop_row.addWidget(self.print_shop_check)
        self.print_shop_info_label = QLabel("ⓘ", self)
        self.print_shop_info_label.setStyleSheet("QLabel { color: #666; }")
        print_shop_row.addWidget(self.print_shop_info_label)
        print_shop_row.addStretch(1)
        root.addLayout(print_shop_row)

        blank_row = QHBoxLayout()
        self.skip_blank_check = QCheckBox(self)
        blank_row.addWidget(self.skip_blank_check)
        self.blank_threshold_label = QLabel(self)
        blank_row.addWidget(self.blank_threshold_label)
        self.blank_threshold_spin = QDoubleSpinBox(self)
        self.blank_threshold_spin.setRange(0, 100)
        self.blank_threshold_spin.setDecimals(1)
        self.blank_threshold_spin.setSingleStep(0.5)
        self.blank_threshold_spin.setValue(0.5)
        self.blank_threshold_spin.setSuffix(" %")
        blank_row.addWidget(self.blank_threshold_spin)
        blank_row.addStretch(1)
        root.addLayout(blank_row)

        self.export_button = QPushButton(self)
        self.export_button.setEnabled(False)
        self.export_button.clicked.connect(self._on_export)
        root.addWidget(self.export_button)

        self.progress_bar = QProgressBar(self)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setVisible(False)
        root.addWidget(self.progress_bar)

        self.status_label = QLabel("", self)
        self.status_label.setWordWrap(True)
        self.status_label.setAccessibleName("Status")
        root.addWidget(self.status_label)

        QShortcut(QKeySequence("Ctrl+O"), self, activated=self.drop_zone._browse)
        QShortcut(QKeySequence("Ctrl+E"), self, activated=self._on_export)

        self.settings = QSettings("GhostPoster", "GhostPoster")
        self._load_settings()
        self._retranslate_ui()

    def _make_crop_coord_spin(self) -> QDoubleSpinBox:
        spin = QDoubleSpinBox(self)
        spin.setRange(0, 100000)
        spin.setDecimals(1)
        spin.setSuffix(" mm")
        spin.setMaximumWidth(90)
        return spin

    # -- Manual Crop -------------------------------------------------------

    def _on_manual_crop_button(self) -> None:
        if self._page_size_pt is None:
            return
        if self.preview._crop_edit_mode:
            self._confirm_manual_crop()
        else:
            self._enter_manual_crop_edit()

    def _enter_manual_crop_edit(self) -> None:
        # jeśli nic jeszcze nie wykryto automatycznie, spróbuj wykryć teraz —
        # zaczynanie od dobrze dopasowanego prostokąta oszczędza ruchy myszą
        initial = self._manual_crop_rect_pt or self._content_bbox_pt
        if initial is None:
            try:
                initial = detect_content_bbox(self._input_path, self.page_spin.value())
                self._content_bbox_pt = initial
            except Exception:  # noqa: BLE001
                initial = None
        self.preview.enable_crop_edit_mode(initial)
        self.manual_crop_button.setText(t(self.lang, "manual_crop_button_confirm"))
        self.manual_crop_hint_label.setText(t(self.lang, "manual_crop_hint"))
        self.manual_crop_hint_label.setVisible(True)
        self._update_manual_crop_size_label(self.preview.crop_rect_pt)

    def _confirm_manual_crop(self) -> None:
        rect = self.preview.crop_rect_pt
        self.preview.disable_crop_edit_mode()
        self.manual_crop_button.setText(t(self.lang, "manual_crop_button"))
        self.manual_crop_hint_label.setVisible(False)
        if rect is not None:
            self._manual_crop_rect_pt = rect
            self._update_manual_crop_spins()
            self._set_manual_crop_controls_enabled(True)
            self._update_manual_crop_size_label(rect)
            self._recompute_tiles()

    def _on_manual_crop_reset(self) -> None:
        self._manual_crop_rect_pt = None
        self.preview.set_crop_rect_pt(None)
        self.preview.disable_crop_edit_mode()
        self.manual_crop_button.setText(t(self.lang, "manual_crop_button"))
        self.manual_crop_hint_label.setVisible(False)
        self.manual_crop_size_label.setVisible(False)
        self._set_manual_crop_controls_enabled(False)
        self._recompute_tiles()

    def _on_preview_crop_rect_changed(self) -> None:
        """Użytkownik puścił przycisk myszy po przeciągnięciu narożnika."""
        rect = self.preview.crop_rect_pt
        if rect is not None:
            self._update_manual_crop_spins(rect)
            self._update_manual_crop_size_label(rect)

    def _on_preview_crop_rect_live(self) -> None:
        """Aktualizacja na żywo podczas przeciągania (przed puszczeniem przycisku)."""
        self._update_manual_crop_size_label(self.preview.crop_rect_pt)

    def _update_manual_crop_size_label(self, rect: fitz.Rect | None) -> None:
        if rect is None:
            self.manual_crop_size_label.setVisible(False)
            return
        self.manual_crop_size_label.setText(
            t(
                self.lang,
                "manual_crop_size_label",
                width=f"{pt_to_mm(rect.width):.0f}",
                height=f"{pt_to_mm(rect.height):.0f}",
            )
        )
        self.manual_crop_size_label.setVisible(True)

    def _on_manual_crop_spin_changed(self, _value: float) -> None:
        if not self.manual_crop_x0_spin.isEnabled():
            return
        rect = fitz.Rect(
            mm_to_pt(self.manual_crop_x0_spin.value()),
            mm_to_pt(self.manual_crop_y0_spin.value()),
            mm_to_pt(self.manual_crop_x1_spin.value()),
            mm_to_pt(self.manual_crop_y1_spin.value()),
        )
        rect.normalize()
        self._manual_crop_rect_pt = rect
        self.preview.set_crop_rect_pt(rect if self.preview._crop_edit_mode else None)
        if not self.preview._crop_edit_mode:
            self._recompute_tiles()

    def _update_manual_crop_spins(self, rect: fitz.Rect | None = None) -> None:
        rect = rect or self._manual_crop_rect_pt
        if rect is None:
            return
        for spin, value_pt in (
            (self.manual_crop_x0_spin, rect.x0),
            (self.manual_crop_y0_spin, rect.y0),
            (self.manual_crop_x1_spin, rect.x1),
            (self.manual_crop_y1_spin, rect.y1),
        ):
            spin.blockSignals(True)
            spin.setValue(pt_to_mm(value_pt))
            spin.blockSignals(False)

    def _set_manual_crop_controls_enabled(self, enabled: bool) -> None:
        self.manual_crop_reset_button.setEnabled(enabled)
        for spin in (
            self.manual_crop_x0_spin,
            self.manual_crop_y0_spin,
            self.manual_crop_x1_spin,
            self.manual_crop_y1_spin,
        ):
            spin.setEnabled(enabled)

    def _show_quick_guide(self) -> None:
        QMessageBox.information(
            self, t(self.lang, "help_action_guide"), t(self.lang, "quick_guide_text")
        )

    def _show_report_bug_dialog(self) -> None:
        msg = QMessageBox(self)
        msg.setWindowTitle(t(self.lang, "help_action_report_bug"))
        msg.setText(t(self.lang, "report_bug_text"))
        open_issues_button = msg.addButton(
            t(self.lang, "report_bug_open_issues"), QMessageBox.ActionRole
        )
        msg.addButton(QMessageBox.Close)
        msg.exec()
        if msg.clickedButton() == open_issues_button:
            QDesktopServices.openUrl(QUrl(f"{PROJECT_URL}/issues/new"))

    def _show_about_dialog(self) -> None:
        QMessageBox.about(
            self,
            t(self.lang, "help_action_about"),
            t(self.lang, "about_text", version=__version__),
        )

    # -- język -----------------------------------------------------------

    def _on_language_changed(self, _index: int) -> None:
        self.lang = self.language_combo.currentData()
        self._retranslate_ui()
        # _retranslate_ui() aktualizuje tylko statyczne napisy (przyciski,
        # checkboxy); status na dole jest generowany dynamicznie i trzeba
        # go przeliczyć ponownie, żeby też zmienił język
        if self._page_size_pt is not None:
            self._recompute_tiles()
        elif self._input_path is not None:
            self.status_label.setText(t(self.lang, "status_ready", path=self._input_path))

    def _retranslate_ui(self) -> None:
        lang = self.lang
        self.setWindowTitle(t(lang, "window_title"))
        self.language_label.setText(t(lang, "language_label"))
        self.help_button.setText(t(lang, "help_link_text"))
        self.action_help_readme.setText(t(lang, "help_action_readme"))
        self.action_help_guide.setText(t(lang, "help_action_guide"))
        self.action_help_github.setText(t(lang, "help_action_github"))
        self.action_help_report_bug.setText(t(lang, "help_action_report_bug"))
        self.action_help_about.setText(t(lang, "help_action_about"))

        self.drop_zone.retranslate(lang)
        self.preview.retranslate(lang)

        self.paper_label.setText(t(lang, "paper_label"))
        self.paper_combo.setAccessibleName(t(lang, "paper_accessible_name"))
        self.overlap_label.setText(t(lang, "overlap_label"))
        self.overlap_spin.setAccessibleName(t(lang, "overlap_accessible_name"))
        self.page_label.setText(t(lang, "page_label"))
        self.page_spin.setAccessibleName(t(lang, "page_accessible_name"))
        self.crop_margin_label.setText(t(lang, "crop_margin_label"))
        self.crop_margin_combo.setAccessibleName(t(lang, "crop_margin_accessible_name"))

        self.manual_crop_button.setText(
            t(
                lang,
                (
                    "manual_crop_button_confirm"
                    if self.preview._crop_edit_mode
                    else "manual_crop_button"
                ),
            )
        )
        self.manual_crop_reset_button.setText(t(lang, "manual_crop_reset_button"))
        if self.preview._crop_edit_mode:
            self.manual_crop_hint_label.setText(t(lang, "manual_crop_hint"))
        if self.manual_crop_size_label.isVisible():
            self._update_manual_crop_size_label(
                self.preview.crop_rect_pt or self._manual_crop_rect_pt
            )
        self.manual_crop_x0_label.setText(t(lang, "manual_crop_x0_label"))
        self.manual_crop_y0_label.setText(t(lang, "manual_crop_y0_label"))
        self.manual_crop_x1_label.setText(t(lang, "manual_crop_x1_label"))
        self.manual_crop_y1_label.setText(t(lang, "manual_crop_y1_label"))

        self.marks_check.setText(t(lang, "marks_check"))
        self.marks_check.setAccessibleName(t(lang, "marks_check"))
        self.marks_check.setToolTip(t(lang, "marks_check_tooltip"))
        self.cutlines_check.setText(t(lang, "cutlines_check"))
        self.cutlines_check.setAccessibleName(t(lang, "cutlines_check"))
        self.cutlines_check.setToolTip(t(lang, "cutlines_check_tooltip"))
        self.labels_check.setText(t(lang, "labels_check"))
        self.labels_check.setAccessibleName(t(lang, "labels_check"))
        self.labels_check.setToolTip(t(lang, "labels_check_tooltip"))
        self.maximize_check.setText(t(lang, "maximize_check"))
        self.maximize_check.setAccessibleName(t(lang, "maximize_check"))
        self.maximize_check.setToolTip(t(lang, "maximize_check_tooltip"))
        self.auto_crop_check.setText(t(lang, "auto_crop_check"))
        self.auto_crop_check.setAccessibleName(t(lang, "auto_crop_check"))
        self.auto_crop_check.setToolTip(t(lang, "auto_crop_check_tooltip"))
        self.letter_per_row_check.setText(t(lang, "letter_per_row_check"))
        self.letter_per_row_check.setAccessibleName(t(lang, "letter_per_row_check"))
        self.letter_per_row_check.setToolTip(t(lang, "letter_per_row_check_tooltip"))
        self.print_shop_check.setText(t(lang, "print_shop_check"))
        self.print_shop_check.setAccessibleName(t(lang, "print_shop_check"))
        self.print_shop_check.setToolTip(t(lang, "print_shop_check_tooltip"))
        self.print_shop_info_label.setToolTip(t(lang, "print_shop_check_tooltip"))
        self.skip_blank_check.setText(t(lang, "skip_blank_check"))
        self.skip_blank_check.setAccessibleName(t(lang, "skip_blank_check"))
        self.skip_blank_check.setToolTip(t(lang, "skip_blank_check_tooltip"))
        self.blank_threshold_label.setText(t(lang, "blank_threshold_label"))
        self.blank_threshold_spin.setAccessibleName(t(lang, "blank_threshold_accessible_name"))

        self.export_button.setText(t(lang, "export_button"))
        self.export_button.setAccessibleName(t(lang, "export_accessible_name"))
        self.progress_bar.setAccessibleName(t(lang, "progress_accessible_name"))

    # -- zapamiętane ustawienia ----------------------------------------

    def _load_settings(self) -> None:
        """Przywraca ostatnio użyte ustawienia (format, zakładka, znaczniki, język)."""
        lang = self.settings.value("language", DEFAULT_LANG, type=str)
        idx = self.language_combo.findData(lang)
        self.language_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.lang = self.language_combo.currentData()

        paper = self.settings.value("paper", "A3", type=str)
        if paper in available_sizes():
            self.paper_combo.setCurrentText(paper)
        self.overlap_spin.setValue(self.settings.value("overlap_mm", 10.0, type=float))
        self.marks_check.setChecked(self.settings.value("marks", False, type=bool))
        self.cutlines_check.setChecked(self.settings.value("cutlines", False, type=bool))
        self.labels_check.setChecked(self.settings.value("labels", False, type=bool))
        self.maximize_check.setChecked(self.settings.value("maximize", False, type=bool))
        self.print_shop_check.setChecked(self.settings.value("print_shop", False, type=bool))
        self.skip_blank_check.setChecked(self.settings.value("skip_blank", False, type=bool))
        self.blank_threshold_spin.setValue(
            self.settings.value("blank_threshold_pct", 0.5, type=float)
        )
        self.auto_crop_check.setChecked(self.settings.value("auto_crop", False, type=bool))
        self.letter_per_row_check.setChecked(
            self.settings.value("letter_per_row", False, type=bool)
        )

        crop_margin = self.settings.value("crop_margin_mm", DEFAULT_CROP_MARGIN_MM, type=int)
        idx = self.crop_margin_combo.findData(crop_margin)
        self.crop_margin_combo.setCurrentIndex(
            idx if idx >= 0 else CROP_MARGIN_OPTIONS_MM.index(DEFAULT_CROP_MARGIN_MM)
        )

    def _save_settings(self) -> None:
        self.settings.setValue("language", self.language_combo.currentData())
        self.settings.setValue("paper", self.paper_combo.currentText())
        self.settings.setValue("overlap_mm", self.overlap_spin.value())
        self.settings.setValue("marks", self.marks_check.isChecked())
        self.settings.setValue("cutlines", self.cutlines_check.isChecked())
        self.settings.setValue("labels", self.labels_check.isChecked())
        self.settings.setValue("maximize", self.maximize_check.isChecked())
        self.settings.setValue("print_shop", self.print_shop_check.isChecked())
        self.settings.setValue("skip_blank", self.skip_blank_check.isChecked())
        self.settings.setValue("blank_threshold_pct", self.blank_threshold_spin.value())
        self.settings.setValue("auto_crop", self.auto_crop_check.isChecked())
        self.settings.setValue("letter_per_row", self.letter_per_row_check.isChecked())
        self.settings.setValue("crop_margin_mm", self.crop_margin_combo.currentData())

    def closeEvent(self, event) -> None:  # noqa: ANN001
        self._save_settings()
        super().closeEvent(event)

    # -- podgląd -----------------------------------------------------

    def _on_file_chosen(self, path: Path) -> None:
        self._input_path = path
        self._content_bbox_pt = None
        self._manual_crop_rect_pt = None
        self._set_manual_crop_controls_enabled(False)
        self.preview.disable_crop_edit_mode()
        self.manual_crop_button.setText(t(self.lang, "manual_crop_button"))
        self.manual_crop_hint_label.setVisible(False)
        self.manual_crop_size_label.setVisible(False)
        self.page_spin.blockSignals(True)
        self.page_spin.setValue(0)
        self.page_spin.blockSignals(False)
        self.export_button.setEnabled(True)
        self._reload_preview_source()

    def _on_page_changed(self, _value: int) -> None:
        if self._input_path is not None:
            self._content_bbox_pt = None
            self._manual_crop_rect_pt = None
            self._set_manual_crop_controls_enabled(False)
            self.preview.disable_crop_edit_mode()
            self.manual_crop_button.setText(t(self.lang, "manual_crop_button"))
            self.manual_crop_hint_label.setVisible(False)
            self.manual_crop_size_label.setVisible(False)
            self._reload_preview_source()

    def _on_auto_crop_toggled(self, _checked: bool) -> None:
        self._recompute_tiles()

    def _on_crop_margin_changed(self, _index: int) -> None:
        self._content_bbox_pt = None
        if self.auto_crop_check.isChecked():
            self._recompute_tiles()

    def _reload_preview_source(self) -> None:
        """Renderuje wybraną stronę PDF od nowa (plik lub numer strony się zmienił)."""
        if self._input_path is None:
            return
        try:
            self._page_size_pt = self.preview.set_source_page(
                self._input_path, self.page_spin.value()
            )
            self.status_label.setText(t(self.lang, "status_ready", path=self._input_path))
        except PageNumberOutOfRangeError as exc:
            self._page_size_pt = None
            self.preview.clear()
            self.status_label.setText(str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            self._page_size_pt = None
            self.preview.clear()
            self.status_label.setText(t(self.lang, "status_page_error", error=exc))
            return
        self._recompute_tiles()

    def _recompute_tiles(self, *_args) -> None:
        """Przelicza tylko siatkę podziału na podstawie już wyrenderowanej strony."""
        if self._page_size_pt is None:
            return
        try:
            paper_w_pt, paper_h_pt = get_paper_size_pt(self.paper_combo.currentText())
        except Exception:  # noqa: BLE001
            return

        crop_note = ""
        offset_x, offset_y = 0.0, 0.0
        if self._manual_crop_rect_pt is not None and not self.preview._crop_edit_mode:
            rect = self._manual_crop_rect_pt
            page_w_pt, page_h_pt = rect.width, rect.height
            offset_x, offset_y = rect.x0, rect.y0
            crop_note = t(self.lang, "manual_crop_active_note")
        elif self.auto_crop_check.isChecked():
            if self._content_bbox_pt is None:
                try:
                    self._content_bbox_pt = detect_content_bbox(
                        self._input_path,
                        self.page_spin.value(),
                        padding_mm=self.crop_margin_combo.currentData(),
                    )
                except Exception as exc:  # noqa: BLE001
                    self.status_label.setText(t(self.lang, "status_auto_crop_failed", error=exc))
                    self.auto_crop_check.setChecked(False)
                    return
            bbox = self._content_bbox_pt
            page_w_pt, page_h_pt = bbox.width, bbox.height
            offset_x, offset_y = bbox.x0, bbox.y0
            crop_note = t(self.lang, "status_crop_note")
        else:
            page_w_pt, page_h_pt = self._page_size_pt

        overlap_pt = mm_to_pt(self.overlap_spin.value())
        label_style = "row" if self.letter_per_row_check.isChecked() else "column"
        try:
            if self.maximize_check.isChecked():
                result = compute_best_grid(
                    page_w_pt, page_h_pt, paper_w_pt, paper_h_pt, overlap_pt, label_style
                )
                tiles = result.tiles
                orientation_key = (
                    "orientation_landscape"
                    if result.orientation == "poziomo"
                    else "orientation_portrait"
                )
                note = t(
                    self.lang,
                    "status_orientation_note",
                    orientation=t(self.lang, orientation_key),
                )
            else:
                tiles = compute_grid(
                    page_w_pt, page_h_pt, paper_w_pt, paper_h_pt, overlap_pt, label_style
                )
                note = ""
        except ValueError as exc:
            self.status_label.setText(str(exc))
            return

        if offset_x or offset_y:
            tiles = translate_tiles(tiles, offset_x, offset_y)

        self.preview.set_tiles(tiles)
        base = t(self.lang, "status_split", count=len(tiles), paper=self.paper_combo.currentText())
        overlap_warning = ""
        if (
            self.marks_check.isChecked() or self.cutlines_check.isChecked()
        ) and self.overlap_spin.value() <= 0:
            overlap_warning = t(self.lang, "status_overlap_zero_marks_note")
        self._set_status(base, note, crop_note, overlap_warning)

    def _set_status(self, base: str, note: str, crop_note: str, overlap_warning: str) -> None:
        """Ustawia pasek statusu; `overlap_warning`, jeśli jest, wyróżnia się
        na czerwono i pulsuje krótko (3 razy), gdy dopiero się pojawił —
        nie miga bez końca (przez wzgląd na dostępność: ciągłe miganie
        elementów interfejsu jest odradzane przez WCAG, u części osób może
        być męczące albo wręcz szkodliwe)."""
        html = html_escape(base) + html_escape(note) + html_escape(crop_note)
        if overlap_warning:
            html += (
                '<span style="color:#c0392b; font-weight:bold;">'
                f"{html_escape(overlap_warning)}</span>"
            )
        self.status_label.setText(html)

        is_active = bool(overlap_warning)
        if is_active and not self._overlap_warning_active:
            self._flash_count = 0
            self._flash_timer.start()
        elif not is_active and self._overlap_warning_active:
            self._flash_timer.stop()
            self.status_label.setStyleSheet("")
        self._overlap_warning_active = is_active

    def _on_status_flash_tick(self) -> None:
        self._flash_count += 1
        if self._flash_count % 2 == 1:
            self.status_label.setStyleSheet("QLabel { background-color: #f8d7da; }")
        else:
            self.status_label.setStyleSheet("")
        if self._flash_count >= 6:  # 3 pelne cykle wl/wyl, ok. 1.3s
            self._flash_timer.stop()
            self.status_label.setStyleSheet("")

    # -- eksport -------------------------------------------------------

    def _on_export(self) -> None:
        if self._input_path is None:
            QMessageBox.information(
                self, t(self.lang, "no_file_title"), t(self.lang, "no_file_body")
            )
            return

        default_out = self._input_path.with_name(f"{self._input_path.stem}_tiled.pdf")
        out_str, _ = QFileDialog.getSaveFileName(
            self, t(self.lang, "save_as_dialog"), str(default_out), "PDF (*.pdf)"
        )
        if not out_str:
            return

        self.export_button.setEnabled(False)
        self.status_label.setText(t(self.lang, "status_processing"))
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(True)

        self._worker = TileWorker(
            input_path=self._input_path,
            output_path=Path(out_str),
            paper=self.paper_combo.currentText(),
            overlap_mm=self.overlap_spin.value(),
            page_number=self.page_spin.value(),
            marks=self.marks_check.isChecked(),
            cutlines=self.cutlines_check.isChecked(),
            labels=self.labels_check.isChecked(),
            maximize=self.maximize_check.isChecked(),
            print_shop=self.print_shop_check.isChecked(),
            skip_blank=self.skip_blank_check.isChecked(),
            auto_crop=self.auto_crop_check.isChecked(),
            crop_margin_mm=self.crop_margin_combo.currentData(),
            blank_threshold_pct=self.blank_threshold_spin.value(),
            manual_crop_rect_pt=(
                (
                    self._manual_crop_rect_pt.x0,
                    self._manual_crop_rect_pt.y0,
                    self._manual_crop_rect_pt.x1,
                    self._manual_crop_rect_pt.y1,
                )
                if self._manual_crop_rect_pt is not None
                else None
            ),
            label_style="row" if self.letter_per_row_check.isChecked() else "column",
        )
        self._worker.progress.connect(self._on_progress)
        self._worker.finished_ok.connect(self._on_success)
        self._worker.failed.connect(self._on_error)
        self._worker.start()

    def _on_progress(self, done: int, total: int) -> None:
        self.progress_bar.setRange(0, total)
        self.progress_bar.setValue(done)
        self.status_label.setText(t(self.lang, "status_progress", done=done, total=total))

    def _on_success(self, written: int, skipped: int, output_path: str) -> None:
        self.export_button.setEnabled(True)
        self.progress_bar.setVisible(False)
        base = t(self.lang, "status_saved", count=written, path=output_path)
        skipped_note = t(self.lang, "status_saved_skipped", count=skipped) if skipped else ""
        self.status_label.setText(f"{base}{skipped_note}")

    def _on_error(self, message: str) -> None:
        self.export_button.setEnabled(True)
        self.progress_bar.setVisible(False)
        self.status_label.setText(t(self.lang, "status_error", message=message))
        QMessageBox.critical(self, t(self.lang, "error_title"), message)


# Splash.png ma tekst "Version X.Y.Z" wypalony na stałe w grafice (obraz
# statyczny, dostarczony raz przez użytkownika). Zamiast ręcznie edytować
# plik przy każdym wydaniu (i nieuchronnie o tym zapominać), przykrywamy
# ten konkretny obszar i dorysowujemy AKTUALNĄ wersję w czasie działania
# programu. Współrzędne wyznaczone raz, analizą pikseli oryginalnego obrazu
# (poszukiwanie niebieskiego tekstu) — jeśli ktoś podmieni cały splash.png
# na inny layout, te liczby trzeba będzie wyznaczyć na nowo tą samą metodą.
_SPLASH_VERSION_BAND = (0, 1100, 1024, 60)  # x, y, width, height
_SPLASH_VERSION_BG = QColor(245, 245, 245)  # tło ma subtelną teksturę (241–249) — to średnia
_SPLASH_VERSION_COLOR = QColor(10, 122, 232)


def _splash_with_current_version(pixmap: QPixmap) -> QPixmap:
    """Zwraca kopię `pixmap` z przykrytym starym numerem wersji i dorysowaną
    aktualną wersją (`ghostposter.__version__`) w tym samym miejscu."""
    result = QPixmap(pixmap)
    painter = QPainter(result)
    painter.setRenderHint(QPainter.Antialiasing)
    x, y, w, h = _SPLASH_VERSION_BAND
    painter.fillRect(x, y, w, h, _SPLASH_VERSION_BG)
    painter.setPen(_SPLASH_VERSION_COLOR)
    font = QFont()
    font.setBold(True)
    font.setPointSize(26)
    painter.setFont(font)
    painter.drawText(x, y, w, h, Qt.AlignCenter, f"Version {__version__}")
    painter.end()
    return result


def main() -> None:
    app = QApplication(sys.argv)

    splash = None
    if SPLASH_IMAGE_PATH.exists():
        pixmap = _splash_with_current_version(QPixmap(str(SPLASH_IMAGE_PATH)))
        if pixmap.height() > 700:
            pixmap = pixmap.scaledToHeight(700, Qt.SmoothTransformation)
        splash = QSplashScreen(pixmap)
        splash.show()
        app.processEvents()

    window = MainWindow()

    def _reveal_main_window() -> None:
        if splash is not None:
            splash.finish(window)
        window.show()

    QTimer.singleShot(SPLASH_DURATION_MS, _reveal_main_window)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
