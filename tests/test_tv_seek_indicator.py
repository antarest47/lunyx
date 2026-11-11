"""
Pedido explícito del usuario para el reproductor de TV: Izquierda/Derecha
(seek ±10s) dejaron de mostrar el overlay completo de controles por sí
solos — antes sí (vía _restart_hide_timer() en cada seek), y por eso
"la interfaz seguía demorando 3 segundos en desaparecer" incluso después
del fix de pausar/reanudar. Ahora, en vez del overlay completo, seek con
el overlay OCULTO solo hace un flash del corner_indicator (doble
triángulo, ver player_icons._draw_seek_forward/_draw_seek_back).

Comportamiento diferencial pedido explícitamente: si el overlay YA está
en pantalla (pausado, o se navegó adentro con Abajo), seek tiene que
seguir comportándose como antes — quedarse visible, reiniciar el
autoocultado de 3s, SIN el flash del indicador (la barra de progreso del
overlay ya reflejaba la nueva posición).

De paso, mouse_moved (nuevo en TVVideoFrame, antes sin manejo de mouse a
propósito) también tiene que mostrar el overlay completo — uno de los
tres disparadores pedidos (junto con Espacio y Abajo, este último ya
cubierto por el mecanismo existente de focusChanged).
"""

import os
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

import tv_player


class SeekIndicatorTest(unittest.TestCase):
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

    def setUp(self):
        # corner_indicator es compartido entre tests de esta clase (ver
        # setUpClass) — un flash() de un test anterior puede seguir
        # "visible" acá (su QTimer de autoocultado, real, no llegó a
        # correr entre tests) sin este reset explícito.
        self.view.corner_indicator.hide()

    def tearDown(self):
        while QApplication.overrideCursor() is not None:
            QApplication.restoreOverrideCursor()

    def test_seek_con_overlay_oculto_no_lo_muestra_solo_flashea_el_indicador(self):
        self.view._hide_controls()
        self.assertFalse(self.view.control_bar_widget.isVisible())

        self.view._on_seek_key_pressed(forward=True)

        self.assertFalse(
            self.view.control_bar_widget.isVisible(),
            "seek con el overlay oculto no debería hacerlo aparecer",
        )
        self.assertTrue(self.view.corner_indicator.isVisible())
        self.assertEqual(self.view.corner_indicator._icon_name, "seek_forward")

    def test_seek_hacia_atras_con_overlay_oculto_usa_el_icono_de_retroceso(self):
        self.view._hide_controls()

        self.view._on_seek_key_pressed(forward=False)

        self.assertFalse(self.view.control_bar_widget.isVisible())
        self.assertEqual(self.view.corner_indicator._icon_name, "seek_back")

    def test_seek_con_overlay_visible_lo_mantiene_visible_sin_flashear_el_indicador(self):
        self.view._show_controls()
        self.assertTrue(self.view.control_bar_widget.isVisible())

        self.view._on_seek_key_pressed(forward=True)

        self.assertTrue(
            self.view.control_bar_widget.isVisible(),
            "seek con el overlay ya visible tiene que dejarlo visible (comportamiento previo)",
        )
        self.assertFalse(
            self.view.corner_indicator.isVisible(),
            "con el overlay visible no hace falta el flash del indicador (ruido redundante)",
        )

    def test_mouse_moved_muestra_el_overlay(self):
        self.view._hide_controls()
        self.assertFalse(self.view.control_bar_widget.isVisible())

        self.view.video_frame.mouse_moved.emit()

        self.assertTrue(
            self.view.control_bar_widget.isVisible(),
            "mover el mouse tiene que mostrar el overlay, uno de los tres disparadores pedidos",
        )


if __name__ == "__main__":
    unittest.main()
