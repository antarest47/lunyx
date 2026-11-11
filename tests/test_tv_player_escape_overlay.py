"""
Valida el comportamiento pedido por el usuario para Escape en el overlay
de controles del modo TV: Escape con el overlay visible lo oculta en el
acto (sin salir del video); Escape sin overlay recién sale del video si
ya pasaron ESCAPE_EXIT_GRACE_MS desde que se ocultó — la ventana de
gracia evita que un Escape que llega una fracción de segundo tarde
(pensado para ocultar el overlay que recién se acaba de autoocultar)
termine sacando de la reproducción por accidente.

Prueba TVPlayerView._handle_escape() directo (el callback que
tv_input.install_escape_back dispara cuando el foco está adentro), sin
pasar por el filtro de eventos global — eso ya lo cubre el resto del
módulo tv_input y no es lo que cambió acá. El reloj se controla con
time.monotonic mockeado para no depender de sleeps reales.
"""

import os
import sys
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

import tv_player


class EscapeOverlayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Una sola instancia de libVLC compartida entre los tests de esta
        # clase (antes: una nueva por test, en setUp/tearDown) — crear y
        # destruir vlc.Instance()/media_player repetidas veces en varios
        # archivos test_tv_*.py de la misma corrida completa
        # (tests/run_all.py) terminó haciendo crashear el intérprete
        # ("Fatal glibc error: tpp.c:83 (__pthread_tpp_change_priority)",
        # no siempre en el mismo archivo — parece acumulativo, no un bug
        # puntual de un test). Cada test sigue estableciendo su propia
        # precondición (_show_controls(), etc.) antes de actuar, así que
        # compartir la vista entre tests de la misma clase es seguro acá.
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

    def setUp(self):
        self._exits = []
        self._exit_slot = lambda: self._exits.append(True)
        self.view.back_requested.connect(self._exit_slot)

    def tearDown(self):
        self.view.back_requested.disconnect(self._exit_slot)

    def test_escape_con_overlay_visible_lo_oculta_sin_salir(self):
        self.view._show_controls()
        self.assertTrue(self.view.control_bar_widget.isVisible())

        self.view._handle_escape()

        self.assertFalse(self.view.control_bar_widget.isVisible())
        self.assertEqual(self._exits, [], "no debería salir del video, solo ocultar el overlay")

    def test_escape_dentro_de_la_gracia_no_sale(self):
        self.view._show_controls()
        with patch("tv_player.time.monotonic", return_value=1000.0):
            self.view._handle_escape()  # oculta el overlay, marca _controls_hidden_at=1000.0
        self.assertFalse(self.view.control_bar_widget.isVisible())

        with patch("tv_player.time.monotonic", return_value=1000.0 + 1.9):
            self.view._handle_escape()

        self.assertEqual(
            self._exits, [],
            "Escape a 1.9s de ocultado el overlay (dentro de los 2s de gracia) no debería salir",
        )

    def test_escape_despues_de_la_gracia_sale(self):
        self.view._show_controls()
        with patch("tv_player.time.monotonic", return_value=1000.0):
            self.view._handle_escape()

        with patch("tv_player.time.monotonic", return_value=1000.0 + 2.1):
            self.view._handle_escape()

        self.assertEqual(
            self._exits, [True],
            "Escape a 2.1s de ocultado el overlay (después de los 2s de gracia) debería salir",
        )


if __name__ == "__main__":
    unittest.main()
