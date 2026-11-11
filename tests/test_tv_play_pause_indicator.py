"""
Pedido explícito del usuario para el reproductor de TV: togglear play/
pausa con Espacio (o Enter/el botón real) tiene que ser simétrico.
Pausar ya mostraba el overlay de controles al toque — eso se mantiene.
Lo nuevo: reanudar ahora OCULTA el overlay al toque en vez de mostrarlo
de nuevo/reiniciar el autoocultado de 3s, y si el overlay ya estaba
oculto (autoocultado tras esos 3s) reanudar no lo hace reaparecer. De
paso, un indicador visual (círculo blanco semitransparente, esquina
superior izquierda) hace un flash breve con el ícono de play o pausa en
cada toggle, para que se note el cambio incluso con el overlay oculto.

Prueba TVPlayerView._on_play_pause_toggled() directo, con get_state()/
play()/pause() mockeados — no depende de que libVLC realmente decodifique
nada, solo del efecto sobre los widgets Qt (control_bar_widget,
corner_indicator), igual que el resto de tests de tv_player.py.
"""

import os
import sys
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

import vlc

import tv_player


class PlayPauseIndicatorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Una sola instancia de libVLC compartida entre los tests de esta
        # clase (antes: una nueva por test) — ver la misma nota en
        # test_tv_player_escape_overlay.py (crear/destruir vlc.Instance()
        # repetidas veces entre varios archivos test_tv_*.py hizo
        # crashear tests/run_all.py con un "Fatal glibc error" en
        # __pthread_tpp_change_priority).
        cls.view = tv_player.TVPlayerView()
        cls.view.resize(1280, 720)
        cls.view.show()
        app.processEvents()

    @classmethod
    def tearDownClass(cls):
        cls.view.hide()
        cls.view.media_player.stop()
        cls.view.media_player.release()
        cls.view._engine.vlc_instance.release()

    def tearDown(self):
        # El stack de overrideCursor de QApplication es GLOBAL, no por
        # instancia — un test que deja el overlay oculto a propósito
        # (como test_reanudar_con_el_overlay_ya_autoocultado...) deja un
        # BlankCursor empujado sin su restore correspondiente. Sin
        # drenarlo acá, ese override sobrevive al siguiente test del
        # archivo y contamina cualquier assert sobre QApplication.overrideCursor().
        while QApplication.overrideCursor() is not None:
            QApplication.restoreOverrideCursor()

    def _set_state(self, state):
        patcher = patch.object(self.view.media_player, "get_state", return_value=state)
        self.addCleanup(patcher.stop)
        patcher.start()

    def test_pausar_muestra_el_overlay_y_hace_flash_de_pausa(self):
        self._set_state(vlc.State.Playing)
        with patch.object(self.view.media_player, "pause"):
            self.view._on_play_pause_toggled()

        self.assertTrue(self.view.control_bar_widget.isVisible())
        self.assertTrue(self.view.corner_indicator.isVisible())
        self.assertEqual(self.view.corner_indicator._icon_name, "pause")

    def test_reanudar_oculta_el_overlay_al_toque_y_hace_flash_de_play(self):
        self.view._show_controls()  # como si acabara de pausar
        self._set_state(vlc.State.Paused)
        with patch.object(self.view.media_player, "play"):
            self.view._on_play_pause_toggled()

        self.assertFalse(
            self.view.control_bar_widget.isVisible(),
            "reanudar tiene que ocultar el overlay al toque, no volver a mostrarlo",
        )
        self.assertTrue(self.view.corner_indicator.isVisible())
        self.assertEqual(self.view.corner_indicator._icon_name, "play")

    def test_reanudar_con_el_overlay_ya_autoocultado_no_lo_hace_reaparecer(self):
        self.view._hide_controls()  # simula el autoocultado real tras los 3s
        self.assertFalse(self.view.control_bar_widget.isVisible())

        self._set_state(vlc.State.Paused)
        with patch.object(self.view.media_player, "play"):
            self.view._on_play_pause_toggled()

        self.assertFalse(
            self.view.control_bar_widget.isVisible(),
            "reanudar con el overlay ya oculto no debería hacerlo reaparecer",
        )

    def test_reanudar_con_overlay_ya_oculto_no_duplica_el_cursor_oculto(self):
        """Regresión del guard de idempotencia en _hide_controls(): sin
        él, dos hide() seguidos (uno del autoocultado real, otro del
        _on_play_pause_toggled de acá) apilaban dos BlankCursor pero
        _show_controls() solo hace un restore — el cursor quedaba
        invisible para siempre la próxima vez que se mostraba el overlay."""
        self.view._hide_controls()
        self.assertTrue(self.view._cursor_hidden)

        self._set_state(vlc.State.Paused)
        with patch.object(self.view.media_player, "play"):
            self.view._on_play_pause_toggled()  # segundo _hide_controls(), debe ser no-op

        self.view._show_controls()
        self.assertFalse(
            QApplication.overrideCursor(),
            "un solo restoreOverrideCursor() tiene que alcanzar para recuperar el cursor real",
        )


if __name__ == "__main__":
    unittest.main()
