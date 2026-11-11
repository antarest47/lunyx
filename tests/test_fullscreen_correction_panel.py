"""
Bug real reportado en vivo (modo desktop): al avanzar de episodio estando
en pantalla completa, el panel de corrección manual ("¿Temporada/episodio/
categoría mal detectado?") reaparecía tapando parte del video, con el
video mismo mostrando un frame deformado/con artefactos — la captura de
pantalla real mostraba el toggle del panel asomando debajo de un cuadro
de video corrupto.

Causa: PlayerView.enter_fullscreen() oculta correction_container a mano,
pero avanzar de episodio en fullscreen NO vuelve a pasar por
enter_fullscreen() — llama de nuevo a play_url() sobre el mismo
PlayerView (ver MainWindow._play_episode_from_player), que dispara
_update_nav_buttons() -> _refresh_correction_panel(), y esa función
recalculaba la visibilidad solo en base a "hay contenido cargado", sin
mirar si seguíamos en fullscreen. Fix: _refresh_correction_panel()
ahora también chequea self._fullscreen_active.
"""

import os
import sys
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

import main


class FullscreenCorrectionPanelTest(unittest.TestCase):
    def setUp(self):
        self.view = main.PlayerView()
        self.view.resize(1280, 720)
        self.view.show()
        app.processEvents()

    def tearDown(self):
        self.view.hide()
        self.view.media_player.stop()
        self.view.media_player.release()
        self.view._engine.vlc_instance.release()

    def _play(self, subpath, title, playlist, index):
        with patch("main.progress_store.get_progress_ms", return_value=None):
            self.view.play_url(
                "", title=title, library_id="lib1", subpath=subpath,
                playlist=playlist, playlist_index=index,
                series_key=("lib1", "Serie"), library="lib1-fake",
            )

    def test_avanzar_episodio_en_fullscreen_no_reaparece_el_panel_de_correccion(self):
        playlist = [(("S01E01.mkv",), "Ep 1"), (("S01E02.mkv",), "Ep 2")]

        self._play(("S01E01.mkv",), "Ep 1", playlist, 0)
        self.assertTrue(
            self.view.correction_container.isVisible(),
            "en modo ventana, con contenido cargado, el panel tiene que estar visible",
        )

        self.view.enter_fullscreen()
        self.assertFalse(self.view.correction_container.isVisible())

        self._play(("S01E02.mkv",), "Ep 2", playlist, 1)  # avanzar de episodio sin salir de fullscreen

        self.assertFalse(
            self.view.correction_container.isVisible(),
            "el panel de corrección no debería reaparecer al avanzar de episodio en pantalla completa",
        )

    def test_al_salir_de_fullscreen_el_panel_vuelve_a_aparecer(self):
        playlist = [(("S01E01.mkv",), "Ep 1")]
        self._play(("S01E01.mkv",), "Ep 1", playlist, 0)
        self.view.enter_fullscreen()
        self.assertFalse(self.view.correction_container.isVisible())

        self.view.exit_fullscreen()

        self.assertTrue(self.view.correction_container.isVisible())


if __name__ == "__main__":
    unittest.main()
