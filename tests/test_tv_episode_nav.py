"""
Guarda contra que vuelva a aparecer el bug de navegación espacial en modo
TV donde el QScrollArea se roba el foco (ver tv_input._move_focus).

Bug real reportado en vivo: en la lista de episodios de TV, Arriba desde
el primer episodio de la temporada tenía que llegar al picker
"Subtítulos por defecto" — en cambio, la selección visualmente
desaparecía. Causa: QScrollArea es focusable por default en Qt (para
poder scrollear con teclado), y _move_focus (búsqueda geométrica genérica
de tv_input.py) no lo excluía de los candidatos — su "centro" (el del
viewport entero) quedaba más cerca del episodio que cualquiera de los
pickers, y como QScrollArea no tiene ningún estilo :focus definido, el
foco caía ahí sin ningún indicio visual.

Fix: tv_input._move_focus excluye QAbstractScrollArea de los candidatos
— cambio en el módulo COMPARTIDO, así que este archivo también reprueba
que la grilla de posters de TVGalleryView (que usa el mismo QScrollArea +
_move_focus) siga navegando bien.
"""

import os
import sys
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

import library_store
import tv_input


def _focus(widget):
    """setFocus() a secas no alcanza si la ventana todavía no es la
    "ventana activa" de Qt — mismo patrón que
    TVGalleryView._reactivate_and_focus, necesario acá porque cada test
    crea su propia ventana top-level.

    Orden importante: show() dispara showEvent(), que en TVEpisodeListView
    y TVGalleryView programa SU PROPIO foco inicial por defecto (Volver /
    la pestaña activa) vía QTimer.singleShot(0, ...) — si se llamara
    widget.setFocus() ANTES de vaciar la cola de eventos, ese singleShot
    pendiente se dispara en el processEvents() de más abajo y pisa el
    foco recién puesto. Por eso acá se drena la cola PRIMERO (deja que el
    foco por defecto de la vista se asiente) y recién DESPUÉS se fuerza
    el foco pedido."""
    window = widget.window()
    window.show()
    window.raise_()
    window.activateWindow()
    app.processEvents()
    widget.setFocus()
    app.processEvents()


class EpisodeListNavigationTest(unittest.TestCase):
    def test_arriba_desde_primer_episodio_llega_a_subtitulos(self):
        import tv_episodes

        view = tv_episodes.TVEpisodeListView(on_back=lambda: None)
        view.resize(1600, 900)

        episodes_list = [
            ((f"S01E{i:02d}.mkv",), f"Episodio {i:02d}") for i in range(1, 4)
        ]
        group = {
            "key": "S01", "label": "Temporada 1", "season_number": 1,
            "poster_bytes": None, "episodes": episodes_list,
            "extras": [], "openings_endings": [],
        }
        with patch("tv_episodes.progress_store.get_progress_fraction", return_value=None):
            view._add_group_card(group, show_poster=True)

        row1 = view._row_by_subpath[("S01E01.mkv",)]
        _focus(row1)
        self.assertIs(QApplication.focusWidget(), row1, "no se pudo enfocar la fila del episodio 1")

        tv_input._move_focus(view, "up")
        app.processEvents()

        focused = QApplication.focusWidget()
        self.assertIsNot(
            focused, view.scroll_area,
            "BUG: el QScrollArea se robó el foco — la selección desaparece visualmente",
        )
        self.assertIs(focused, view.subtitle_picker)


class GalleryGridNavigationTest(unittest.TestCase):
    """Mismo tv_input._move_focus, otro consumidor — confirma que
    excluir QAbstractScrollArea no le rompió la navegación a la grilla de
    posters (que también vive adentro de un QScrollArea)."""

    def _make_view_with_grid(self):
        with patch.object(library_store, "list_libraries", return_value=[]):
            import tv_gallery
            view = tv_gallery.TVGalleryView()
        view.resize(1600, 900)
        view._library_by_id = {}
        for i in range(8):
            tile = tv_gallery.TVPosterTile(f"Serie {i}", None, on_click=lambda: None)
            row, col = divmod(i, tv_gallery.TV_GRID_COLUMNS)
            is_last = col == tv_gallery.TV_GRID_COLUMNS - 1 or i == 7
            tile._tv_focus_override = view._poster_focus_override(row, col, is_last)
            view.grid_layout.addWidget(tile, row, col)
            view._grid_tiles[(row, col)] = tile
        return view

    def test_navegacion_dentro_de_la_grilla_sigue_intacta(self):
        view = self._make_view_with_grid()
        tile_0_0 = view._grid_tiles[(0, 0)]
        _focus(tile_0_0)
        self.assertIs(QApplication.focusWidget(), tile_0_0, "no se pudo enfocar el primer tile")

        tv_input._move_focus(view, "down")
        app.processEvents()
        self.assertIs(QApplication.focusWidget(), view._grid_tiles[(1, 0)])

        tv_input._move_focus(view, "right")
        app.processEvents()
        self.assertIs(QApplication.focusWidget(), view._grid_tiles[(1, 1)])


if __name__ == "__main__":
    unittest.main()
