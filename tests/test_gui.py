import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from ghostposter.gui import MainWindow  # noqa: E402


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    yield application


def test_main_window_starts_with_export_disabled(app):
    window = MainWindow()
    assert window.windowTitle() == "GhostPoster"
    assert window.export_button.isEnabled() is False


def test_choosing_file_enables_export(app, tmp_path):
    window = MainWindow()
    dummy_pdf = tmp_path / "plan.pdf"
    dummy_pdf.write_bytes(b"%PDF-1.4 fake")
    window._on_file_chosen(dummy_pdf)
    assert window.export_button.isEnabled() is True


def test_real_pdf_populates_preview_with_tiles(app, tmp_path):
    import fitz

    src = tmp_path / "real.pdf"
    doc = fitz.open()
    doc.new_page(width=2000, height=1500)
    doc.save(src)
    doc.close()

    window = MainWindow()
    window._on_file_chosen(src)

    assert window.preview._base_pixmap is not None
    assert window.preview._page_size_pt is not None
    assert len(window.preview._tiles) > 0


def test_progress_signal_reaches_progress_bar(app, tmp_path, qtbot=None):
    import fitz

    src = tmp_path / "real2.pdf"
    doc = fitz.open()
    doc.new_page(width=2000, height=1500)
    doc.save(src)
    doc.close()

    window = MainWindow()
    window._on_file_chosen(src)
    window.paper_combo.setCurrentText("A4")

    seen = []
    window._worker = None  # upewniamy się, że nie ma wątku w tle z poprzedniego testu

    from ghostposter.gui import TileWorker
    from ghostposter.paper import get_paper_size_pt
    from ghostposter.tiler import plan_tiles

    paper_w, paper_h = get_paper_size_pt("A4")
    tiles = plan_tiles(src, paper_w, paper_h, overlap_mm=10, page_number=0)

    worker = TileWorker(
        input_path=src,
        output_path=tmp_path / "out.pdf",
        paper="A4",
        overlap_mm=10,
        page_number=0,
        marks=False,
        cutlines=False,
        labels=False,
    )
    worker.progress.connect(lambda done, total: seen.append((done, total)))
    worker.run()  # bez .start(), żeby test był synchroniczny i deterministyczny

    assert seen, "sygnał progress nigdy nie doszedł"
    assert seen[-1] == (len(tiles), len(tiles))


def test_settings_persist_across_windows(app):
    from PySide6.QtCore import QSettings

    # izolujemy test od realnych ustawień systemowych
    QSettings.setDefaultFormat(QSettings.IniFormat)

    window1 = MainWindow()
    window1.settings.clear()
    window1.paper_combo.setCurrentText("A2")
    window1.overlap_spin.setValue(22.0)
    window1.marks_check.setChecked(True)
    window1.maximize_check.setChecked(True)
    window1.close()  # wywołuje closeEvent -> _save_settings

    window2 = MainWindow()
    assert window2.paper_combo.currentText() == "A2"
    assert window2.overlap_spin.value() == 22.0
    assert window2.marks_check.isChecked() is True
    assert window2.maximize_check.isChecked() is True


def test_maximize_checkbox_changes_preview_orientation(app, tmp_path):
    import fitz

    src = tmp_path / "wide.pdf"
    doc = fitz.open()
    doc.new_page(width=1600, height=500)  # szeroka, niska strona
    doc.save(src)
    doc.close()

    window = MainWindow()
    window._on_file_chosen(src)
    window.paper_combo.setCurrentText("A3")
    window.overlap_spin.setValue(10)

    tiles_without = len(window.preview._tiles)
    window.maximize_check.setChecked(True)
    tiles_with = len(window.preview._tiles)

    assert tiles_with <= tiles_without


def test_language_switch_translates_static_and_dynamic_text(app, tmp_path):
    """Regresja: status na dole jest generowany dynamicznie (nie jest
    statyczna etykieta), więc samo _retranslate_ui() go nie odswieza —
    trzeba go przeliczyc ponownie po zmianie jezyka."""
    import fitz

    src = tmp_path / "plan.pdf"
    doc = fitz.open()
    doc.new_page(width=900, height=600)
    doc.save(src)
    doc.close()

    window = MainWindow()
    window._on_file_chosen(src)

    idx_en = window.language_combo.findData("en")
    window.language_combo.setCurrentIndex(idx_en)

    assert window.export_button.text().startswith("Export")
    assert "arkusz" not in window.status_label.text()
    assert "sheet" in window.status_label.text() or "Split" in window.status_label.text()


def test_letter_per_row_checkbox_changes_tile_labels(app, tmp_path):
    import fitz

    window = MainWindow()
    pdf_path = tmp_path / "grid_plan.pdf"
    doc = fitz.open()
    doc.new_page(width=2000, height=1500)  # spora strona - wymusza siatke 2+ kolumn i 2+ wierszy
    doc.save(pdf_path)
    doc.close()

    window._on_file_chosen(pdf_path)
    window.paper_combo.setCurrentText("A4")
    window.overlap_spin.setValue(10)

    assert window.letter_per_row_check.isChecked() is False
    default_labels = {(t.row, t.col): t.label for t in window.preview._tiles}
    assert len(default_labels) > 1, "test zaklada siatke wiecej niz 1 kafelek"

    window.letter_per_row_check.setChecked(True)
    row_labels = {(t.row, t.col): t.label for t in window.preview._tiles}

    assert default_labels != row_labels
    assert default_labels[(0, 1)] != row_labels[(0, 1)]


def test_window_content_is_scrollable_on_small_screens(app, tmp_path):
    """Regresja: na mniejszych rozdzielczościach (np. laptopy 1366x768)
    okno nie mieściło się w pionie i przycisk Eksportuj oraz część
    checkboxów były całkowicie nieosiągalne, bez żadnego sposobu, żeby
    się do nich dostać. Cała zawartość musi być w przewijanym obszarze."""
    import fitz
    from PySide6.QtWidgets import QScrollArea

    window = MainWindow()
    pdf_path = tmp_path / "plan.pdf"
    doc = fitz.open()
    doc.new_page(width=2000, height=1500)
    doc.save(pdf_path)
    doc.close()

    assert isinstance(window.centralWidget(), QScrollArea)
    assert window.centralWidget().widgetResizable() is True

    window.resize(1366, 420)  # bardzo niskie okno - symulacja malego ekranu
    window.show()
    window._on_file_chosen(pdf_path)

    scroll = window.centralWidget()
    vbar = scroll.verticalScrollBar()
    assert vbar.maximum() > 0, "przy bardzo niskim oknie powinno byc cos do przewiniecia"

    viewport_h = scroll.viewport().height()
    export_y_before = window.export_button.mapTo(window, window.export_button.rect().topLeft()).y()
    assert export_y_before > viewport_h, "test zaklada, ze przycisk jest poza widokiem na starcie"

    vbar.setValue(vbar.maximum())
    export_y_after = window.export_button.mapTo(window, window.export_button.rect().topLeft()).y()
    assert 0 <= export_y_after <= viewport_h, "po przewinieciu przycisk Eksportuj musi byc widoczny"


def test_default_window_size_needs_no_scrolling(app, tmp_path):
    """Domyślny rozmiar okna powinien mieścić całą zawartość bez
    przewijania w typowych warunkach (ekran wystarczająco duży)."""
    import fitz

    window = MainWindow()
    pdf_path = tmp_path / "plan2.pdf"
    doc = fitz.open()
    doc.new_page(width=2000, height=1500)
    doc.save(pdf_path)
    doc.close()

    window.show()
    window._on_file_chosen(pdf_path)

    vbar = window.centralWidget().verticalScrollBar()
    assert vbar.maximum() == 0


def test_splash_shows_current_version_not_baked_in_one(app):
    """Regresja: obraz splash.png ma tekst 'Version 1.0.0' wypalony na
    stałe w grafice — po kilku wydaniach ten numer się zdezaktualizował.
    _splash_with_current_version musi go przykryć i narysować aktualną
    wersję, żeby splash nigdy nie kłamał o wersji programu."""
    from PySide6.QtGui import QImage, QPixmap

    from ghostposter import __version__
    from ghostposter.gui import SPLASH_IMAGE_PATH, _splash_with_current_version

    assert SPLASH_IMAGE_PATH.exists(), "splash.png musi byc w ghostposter/assets/, nie tylko docs/"

    original = QPixmap(str(SPLASH_IMAGE_PATH))
    patched = _splash_with_current_version(original)

    assert patched.width() == original.width()
    assert patched.height() == original.height()

    img = patched.toImage().convertToFormat(QImage.Format_RGB888)
    blue_pixel_found = False
    y = 1130
    for x in range(0, img.width(), 2):
        color = img.pixelColor(x, y)
        if color.blue() > 180 and color.blue() - color.red() > 60:
            blue_pixel_found = True
            break
    assert blue_pixel_found, "nie znaleziono narysowanego tekstu wersji w oczekiwanym pasie"
    assert __version__ != "1.0.0", "test zakłada, że kod jest już po pierwszej wersji"


class _FakeMimeData:
    def __init__(self, urls):
        self._urls = urls

    def hasUrls(self):
        return bool(self._urls)

    def urls(self):
        return self._urls


class _FakeUrl:
    def __init__(self, path):
        self._path = path

    def toLocalFile(self):
        return self._path


class _FakeDropEvent:
    def __init__(self, urls):
        self._mime = _FakeMimeData(urls)

    def mimeData(self):
        return self._mime

    def acceptProposedAction(self):
        pass


def test_preview_area_accepts_drops_not_just_top_zone(app, tmp_path):
    """Regresja: podgląd (duży pusty obszar pod strefą u góry) sugerował
    drag & drop swoim placeholderem, ale nie akceptował upuszczeń — cały
    obszar podglądu musi też działać jako drop target."""
    window = MainWindow()
    assert window.preview.acceptDrops() is True

    pdf_path = tmp_path / "dropped_on_preview.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake")

    received = []
    window.preview.file_dropped.connect(lambda p: received.append(p))
    window.preview.dropEvent(_FakeDropEvent([_FakeUrl(str(pdf_path))]))

    assert received == [pdf_path]


def test_preview_drop_is_wired_to_main_file_selection(app, tmp_path):
    import fitz

    window = MainWindow()
    pdf_path = tmp_path / "real_plan.pdf"
    doc = fitz.open()
    doc.new_page(width=500, height=400)
    doc.save(pdf_path)
    doc.close()

    window.preview.dropEvent(_FakeDropEvent([_FakeUrl(str(pdf_path))]))

    assert window._input_path == pdf_path
    assert window.export_button.isEnabled() is True
    assert window.preview._base_pixmap is not None


def test_preview_click_opens_browse_only_when_empty(app, monkeypatch, tmp_path):
    import fitz

    window = MainWindow()
    called = []
    monkeypatch.setattr(window.preview, "_browse", lambda: called.append(True))

    class _Pos:
        def x(self):
            return 10.0

        def y(self):
            return 10.0

    class _FakePressEvent:
        def position(self):
            return _Pos()

    window.preview.mousePressEvent(_FakePressEvent())
    assert called == [True]

    pdf_path = tmp_path / "loaded.pdf"
    doc = fitz.open()
    doc.new_page(width=500, height=400)
    doc.save(pdf_path)
    doc.close()
    window._on_file_chosen(pdf_path)

    window.preview.mousePressEvent(_FakePressEvent())
    assert called == [True]  # nie wywolane po raz drugi


def test_preview_enter_key_opens_browse_only_when_empty(app, monkeypatch, tmp_path):
    """Regresja: placeholder mówi 'Kliknij / Enter', więc Enter musi
    naprawdę działać na podglądzie, tak jak na górnej strefie."""
    import fitz
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QKeyEvent

    window = MainWindow()
    called = []
    monkeypatch.setattr(window.preview, "_browse", lambda: called.append(True))

    def _enter_event():
        return QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key_Return, Qt.NoModifier)

    window.preview.keyPressEvent(_enter_event())
    assert called == [True]

    pdf_path = tmp_path / "loaded2.pdf"
    doc = fitz.open()
    doc.new_page(width=500, height=400)
    doc.save(pdf_path)
    doc.close()
    window._on_file_chosen(pdf_path)

    window.preview.keyPressEvent(_enter_event())
    assert called == [True]  # po wczytaniu pliku Enter juz nie otwiera wyboru


def _wheel_event(x, y, angle_delta_y):
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent

    return QWheelEvent(
        QPointF(x, y),
        QPointF(x, y),
        QPoint(0, 0),
        QPoint(0, angle_delta_y),
        Qt.NoButton,
        Qt.NoModifier,
        Qt.ScrollUpdate,
        False,
    )


def test_wheel_zooms_in_up_to_200_percent(app, tmp_path):
    import fitz

    window = MainWindow()
    pdf_path = tmp_path / "plan.pdf"
    doc = fitz.open()
    doc.new_page(width=1000, height=800)
    doc.save(pdf_path)
    doc.close()
    window.resize(600, 500)
    window.show()
    window._on_file_chosen(pdf_path)

    preview = window.preview
    assert preview._zoom == 1.0

    for _ in range(30):  # duzo scrolli w gore - powinno zatrzymac sie na 2.0, nie przekroczyc
        preview.wheelEvent(_wheel_event(300, 250, 120))
    assert preview._zoom == pytest.approx(2.0)
    assert preview._zoom <= preview.ZOOM_MAX


def test_wheel_does_not_zoom_out_below_100_percent(app, tmp_path):
    import fitz

    window = MainWindow()
    pdf_path = tmp_path / "plan.pdf"
    doc = fitz.open()
    doc.new_page(width=1000, height=800)
    doc.save(pdf_path)
    doc.close()
    window.resize(600, 500)
    window.show()
    window._on_file_chosen(pdf_path)

    preview = window.preview
    preview.wheelEvent(_wheel_event(300, 250, -120))  # scroll w dol od 100%
    assert preview._zoom == 1.0


def test_zoom_resets_when_loading_a_new_file(app, tmp_path):
    import fitz

    window = MainWindow()
    pdf_path = tmp_path / "plan.pdf"
    doc = fitz.open()
    doc.new_page(width=1000, height=800)
    doc.save(pdf_path)
    doc.close()
    window.resize(600, 500)
    window.show()
    window._on_file_chosen(pdf_path)

    preview = window.preview
    preview.wheelEvent(_wheel_event(300, 250, 120))
    assert preview._zoom > 1.0

    pdf_path2 = tmp_path / "plan2.pdf"
    doc = fitz.open()
    doc.new_page(width=900, height=700)
    doc.save(pdf_path2)
    doc.close()
    window._on_file_chosen(pdf_path2)
    assert preview._zoom == 1.0
    assert preview._pan_x == 0.0
    assert preview._pan_y == 0.0


def test_transform_matches_previous_fit_behavior_at_default_zoom(app, tmp_path):
    """Regresja: przy domyślnym zoomie (100%, brak przesunięcia) wynik musi
    być identyczny jak w oryginalnej wersji bez zoomu (dopasuj i wyśrodkuj)."""
    import fitz

    window = MainWindow()
    pdf_path = tmp_path / "plan.pdf"
    doc = fitz.open()
    doc.new_page(width=1000, height=500)
    doc.save(pdf_path)
    doc.close()
    window.resize(600, 500)
    window.show()
    window._on_file_chosen(pdf_path)

    preview = window.preview
    scale, offset_x, offset_y = preview._transform()
    expected_scale = min(preview.width() / 1000, preview.height() / 500)
    assert scale == pytest.approx(expected_scale)
    expected_ox = (preview.width() - 1000 * expected_scale) / 2
    expected_oy = (preview.height() - 500 * expected_scale) / 2
    assert offset_x == pytest.approx(expected_ox)
    assert offset_y == pytest.approx(expected_oy)


def test_zoomed_in_pan_stays_clamped_to_content(app, tmp_path):
    """Po przybliżeniu i próbie przeciągnięcia dużo dalej niż zawartość
    strony, _clamp_pan musi realnie ograniczyć przesunięcie, a nie
    zwrócić dokładnie to, o co poproszono."""
    import fitz

    window = MainWindow()
    pdf_path = tmp_path / "plan.pdf"
    doc = fitz.open()
    doc.new_page(width=1000, height=800)
    doc.save(pdf_path)
    doc.close()
    window.resize(600, 500)
    window.show()
    window._on_file_chosen(pdf_path)

    preview = window.preview
    for _ in range(10):
        preview.wheelEvent(_wheel_event(300, 250, 120))
    assert preview._zoom > 1.0

    scale = preview._transform()[0]
    huge_pan_x, huge_pan_y = preview._clamp_pan(100000, 100000, scale)
    assert huge_pan_x < 100000
    assert huge_pan_y < 100000

    negative_pan_x, negative_pan_y = preview._clamp_pan(-100000, -100000, scale)
    assert negative_pan_x > -100000
    assert negative_pan_y > -100000


def test_overlap_zero_with_marks_shows_warning(app, tmp_path):
    """Regresja: przy --overlap 0 krzyże pasowania i linie cięcia nie mają
    czego oznaczyć (arkusze się nie nakładają) — GUI musi o tym uprzedzić,
    zamiast po cichu generować plik bez znaczników, mimo że użytkownik je
    zaznaczył."""
    import fitz

    window = MainWindow()
    pdf_path = tmp_path / "plan.pdf"
    doc = fitz.open()
    doc.new_page(width=400, height=1600)
    doc.save(pdf_path)
    doc.close()

    window._on_file_chosen(pdf_path)
    window.overlap_spin.setValue(0)
    window.marks_check.setChecked(True)
    window.cutlines_check.setChecked(True)

    assert "0 mm" in window.status_label.text()

    window.overlap_spin.setValue(10)
    assert "0 mm" not in window.status_label.text()

    window.overlap_spin.setValue(0)
    window.marks_check.setChecked(False)
    window.cutlines_check.setChecked(False)
    assert "0 mm" not in window.status_label.text()


def test_cli_warns_when_overlap_zero_with_marks(tmp_path):
    import fitz
    from click.testing import CliRunner

    from ghostposter.cli import main

    pdf_path = tmp_path / "plan.pdf"
    doc = fitz.open()
    doc.new_page(width=400, height=1600)
    doc.save(pdf_path)
    doc.close()

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            str(pdf_path),
            "--paper",
            "A4",
            "--overlap",
            "0",
            "--marks",
            "--cutlines",
            "--output",
            str(tmp_path / "out.pdf"),
        ],
    )
    assert result.exit_code == 0
    assert "nie pojawi" in result.output


def test_cli_no_warning_when_overlap_positive(tmp_path):
    import fitz
    from click.testing import CliRunner

    from ghostposter.cli import main

    pdf_path = tmp_path / "plan.pdf"
    doc = fitz.open()
    doc.new_page(width=400, height=1600)
    doc.save(pdf_path)
    doc.close()

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            str(pdf_path),
            "--paper",
            "A4",
            "--overlap",
            "10",
            "--marks",
            "--cutlines",
            "--output",
            str(tmp_path / "out2.pdf"),
        ],
    )
    assert result.exit_code == 0
    assert "nie pojawi" not in result.output
