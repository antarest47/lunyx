"""
Guarda contra que vuelva a perderse el foco de teclado en modo TV al
volver de una película abierta desde "Continuar viendo" (ver
tv_gallery.TVGalleryView.refresh_continue_watching).

Bug real reportado en vivo: refresh_continue_watching() reconstruye los
tiles de la grilla (_render_grid destruye y recrea todo) sin restaurar el
foco después — el tile que tenía el foco antes de reproducir quedaba
destruido, Qt limpiaba el foco de la app a None, y el "cursor" de
selección por teclado quedaba invisible/inmóvil hasta apretar Tab (que
caía en el buscador).

Este test no arma una ventana real ni dispara la navegación por teclado
— verifica el contrato más chico y suficiente: que refresh_continue_watching()
SIEMPRE termina llamando _reactivate_and_focus() con un widget real,
tanto si la grilla se re-renderiza como si "Continuar viendo" desaparece
y cae a la primera biblioteca.
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

import library_store


def _make_view():
    # list_libraries() parcheado a vacío: TVGalleryView.__init__ no debe
    # tocar ningún libraries.json real durante el test.
    with patch.object(library_store, "list_libraries", return_value=[]):
        import tv_gallery
        return tv_gallery, tv_gallery.TVGalleryView()


class ContinueWatchingFocusTest(unittest.TestCase):
    def test_grilla_sigue_visible_restaura_el_foco(self):
        tv_gallery, view = _make_view()
        view._viewing_continue_watching = True
        with patch.object(view, "_render_grid") as render_grid, \
             patch.object(view, "_reactivate_and_focus") as reactivate, \
             patch.object(view, "_start_progress_refresh"), \
             patch.object(view, "_update_continue_watching_tab_visibility"), \
             patch.object(view._continue_watching_button, "isVisible", return_value=True):
            view.refresh_continue_watching()
            # Con _viewing_continue_watching=True y el botón visible (los
            # dos parcheados en este bloque), _active_tab_button() tiene
            # que resolver al botón de "Continuar viendo" — se calcula
            # ACÁ ADENTRO, mientras el parche de isVisible sigue activo:
            # afuera del `with`, isVisible() vuelve a su valor real
            # (False, widget nunca mostrado) y _active_tab_button() daría
            # otra cosa, invalidando la comparación.
            expected_target = view._active_tab_button()

        render_grid.assert_called_once()
        self.assertIs(expected_target, view._continue_watching_button)
        reactivate.assert_called_once_with(expected_target)

    def test_continuar_viendo_desaparece_tambien_restaura_el_foco(self):
        """Si el progreso llegó a cero (todo terminado/borrado) mientras
        se reproducía, la pestaña "Continuar viendo" puede desaparecer y
        la vista cae a la primera biblioteca — ese camino TAMBIÉN destruye
        tiles (_on_library_selected) y necesita la misma restauración."""
        tv_gallery, view = _make_view()
        view._viewing_continue_watching = True
        fake_lib = MagicMock()
        fake_lib.id = "lib1"
        view._library_by_id = {"lib1": fake_lib}
        view._tab_buttons = {"lib1": MagicMock()}
        with patch.object(view, "_on_library_selected") as on_lib_selected, \
             patch.object(view, "_reactivate_and_focus") as reactivate, \
             patch.object(view, "_start_progress_refresh"), \
             patch.object(view, "_update_continue_watching_tab_visibility"), \
             patch.object(view._continue_watching_button, "isVisible", return_value=False):
            view.refresh_continue_watching()

        on_lib_selected.assert_called_once()
        reactivate.assert_called_once()


if __name__ == "__main__":
    unittest.main()
