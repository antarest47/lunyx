"""
Pedido explícito del usuario: al abrir un video (desde la galería o
"continuar viendo"), la GUI completa ya NO aparece por 3 segundos — eso
tapaba subtítulos durante ese tiempo, mismo problema de fondo que el
overlay de controles (Paso 43), pero en el momento de abrir en vez de
por inactividad. En cambio, el título (+ "retomando desde X:XX" si
corresponde) se muestra como marquee de VLC (dibujado por libVLC mismo
sobre el video, transparencia real, sin depender de compositor — a
diferencia de TVCornerIndicator, que terminó necesitando una caja
opaca detrás porque WA_TranslucentBackground resultó poco confiable en
la máquina real del usuario).

Prueba TVPlayerView.play_url() con video_set_marquee_string/int
mockeados — no depende de que libVLC realmente renderice nada (el
propio módulo marq de VLC ya se confirmó en vivo, corriendo `vlc -p marq
--advanced --help-verbose`, que no soporta contorno/borde — aceptado a
propósito, pedido explícito del usuario), solo de qué parámetros les
pasa el código, mismo criterio que el resto de tests de tv_player.py.
"""

import os
import sys
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QPushButton, QVBoxLayout, QWidget

app = QApplication.instance() or QApplication([])

import vlc

import tv_player


class OpenTitleMarqueeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Una sola instancia de libVLC compartida entre los 4 tests de
        # esta clase (setUpClass/tearDownClass, no setUp/tearDown por
        # test) — crear+destruir vlc.Instance()/media_player en cada
        # método, sumado a los que ya crean el resto de los archivos
        # test_tv_*.py, hizo crashear (core dump) tests/run_all.py
        # corriendo la suite completa. Acá no hace falta un media_player
        # nuevo por test: _engine.play_url() está mockeado (ver _play()),
        # nunca se abre nada real.
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
        # _cursor_hidden es un flag de instancia de self.view (compartida
        # entre tests de esta clase) — sin resetearlo, un test anterior
        # que ya ocultó el cursor deja el guard de idempotencia de
        # _hide_cursor() bloqueando el push de ESTE test, aunque
        # tearDown() ya haya drenado el override real de QApplication.
        self.view._cursor_hidden = False

    def tearDown(self):
        while QApplication.overrideCursor() is not None:
            QApplication.restoreOverrideCursor()

    def _play(self, title, resume_ms=None):
        # _engine.play_url() (el open REAL de libVLC) mockeado a propósito
        # acá — repetir aperturas reales con una URL vacía en varios
        # métodos de test seguidos (cada uno con su propia instancia de
        # libVLC, creada/destruida en setUp/tearDown) hizo crashear el
        # intérprete (core dump) corriendo el archivo completo, algo que
        # no pasaba en otros tests porque ninguno otro abre una URL
        # bogus CUATRO veces seguidas en instancias de libVLC distintas.
        # Lo que se prueba acá es la lógica Qt/marquee de play_url(), no
        # el open real de libVLC (eso ya lo cubre
        # test_playback_open_watchdog.py).
        with patch("tv_player.progress_store.get_progress_ms", return_value=resume_ms), \
             patch.object(self.view._engine, "play_url"), \
             patch.object(self.view.media_player, "video_set_marquee_string") as mock_str, \
             patch.object(self.view.media_player, "video_set_marquee_int") as mock_int:
            self.view.play_url(
                "", title=title, library_id="lib1", subpath=("ep.mkv",), library="lib1-fake",
            )
        return mock_str, mock_int

    def test_abrir_hace_flash_del_indicador_con_icono_de_play(self):
        self._play("Episodio 01")
        self.assertTrue(self.view.corner_indicator.isVisible())
        self.assertEqual(self.view.corner_indicator._icon_name, "play")

    def test_abrir_oculta_el_cursor(self):
        """Bug real reportado en vivo: sin overlay que se autooculte
        (Paso 45), nada ocultaba el cursor del mouse hasta la primera
        vez que aparecía el overlay por algún otro motivo — se quedaba
        visible desde el arranque."""
        self._play("Episodio 01")
        self.assertTrue(self.view._cursor_hidden)
        self.assertEqual(QApplication.overrideCursor().shape(), Qt.CursorShape.BlankCursor)

    def test_mouse_moved_justo_despues_de_abrir_no_muestra_el_overlay(self):
        """Bug real reportado en vivo (no reproducible en este entorno
        sin mouse/pantalla real): video_frame puede cambiar de tamaño/
        posición justo al entrar a la vista, y eso puede generar un
        mouseMoveEvent sintético en Qt bajo un cursor que en los hechos
        no se movió — sin la ventana de gracia, ese evento fantasma
        volvía a mostrar el overlay completo apenas se abría el video."""
        self.view._hide_controls()
        self._play("Episodio 01")
        self.assertFalse(self.view.control_bar_widget.isVisible())

        self.view.video_frame.mouse_moved.emit()

        self.assertFalse(
            self.view.control_bar_widget.isVisible(),
            "un mouse_moved inmediatamente después de abrir no debería mostrar el overlay",
        )

    def test_mouse_moved_despues_de_la_gracia_si_muestra_el_overlay(self):
        self.view._hide_controls()
        self._play("Episodio 01")

        with patch("tv_player.time.monotonic", return_value=time.monotonic() + 10):
            self.view.video_frame.mouse_moved.emit()

        self.assertTrue(
            self.view.control_bar_widget.isVisible(),
            "un mouse_moved genuino, ya pasada la ventana de gracia, sí debería mostrar el overlay",
        )

    def test_prepare_for_open_suprime_el_foco_del_cambio_de_stack(self):
        """La causa real del bug reportado en vivo, encontrada con un
        rastreo real (traceback.print_stack() en _show_controls()):
        tv_main.py._play() hace self.stack.setCurrentWidget(self.player_view)
        ANTES de play_url() — y ESE setCurrentWidget por sí solo ya
        dispara un cambio de foco real que landea adentro de la vista
        (Qt restaura el foco al volver a mostrar la página), mucho antes
        de que play_url() llegue a correr. El primer intento de este fix
        (guardar self._auto_hiding recién adentro de play_url(),
        alrededor de su propio setFocus()) no alcanzaba porque llegaba
        tarde — el show ya había pasado. prepare_for_open() (llamado por
        tv_main.py ANTES del setCurrentWidget) cierra esa ventana desde
        antes."""
        self.view.prepare_for_open()

        self.view._on_focus_changed(None, self.view.video_frame)

        self.assertFalse(
            self.view.control_bar_widget.isVisible(),
            "el cambio de foco del setCurrentWidget (simulado acá) no debería mostrar el overlay",
        )

    def test_despues_de_terminar_de_abrir_el_foco_normal_sigue_mostrando_el_overlay(self):
        self.view._hide_controls()
        self.view.prepare_for_open()
        self._play("Episodio 01")  # deja _auto_hiding en False al terminar

        self.view._on_focus_changed(None, self.view.play_pause_button)

        self.assertTrue(
            self.view.control_bar_widget.isVisible(),
            "un cambio de foco genuino DESPUÉS de terminar de abrir (ej. flecha Abajo) "
            "sigue teniendo que mostrar el overlay — no romper ese caso",
        )

    def test_abrir_sin_resumen_muestra_solo_el_titulo_en_el_marquee(self):
        mock_str, mock_int = self._play("Episodio 01", resume_ms=None)
        mock_str.assert_called_once_with(vlc.VideoMarqueeOption.Text, "Episodio 01")
        mock_int.assert_any_call(vlc.VideoMarqueeOption.Enable, 1)
        mock_int.assert_any_call(vlc.VideoMarqueeOption.Timeout, tv_player.HIDE_CONTROLS_DELAY_MS)

    def test_abrir_retomando_incluye_el_tiempo_en_el_marquee(self):
        mock_str, _mock_int = self._play("Episodio 02", resume_ms=312000)  # 5:12
        mock_str.assert_called_once_with(
            vlc.VideoMarqueeOption.Text, "Episodio 02 (retomando desde 5:12)"
        )


class OpenTitleMarqueeFocusRegressionTest(unittest.TestCase):
    """Aparte de OpenTitleMarqueeTest a propósito: este caso necesita
    simular el foco viniendo de AFUERA de TVPlayerView, como pasa de
    verdad al abrir un video desde la galería — un widget señuelo tiene
    que ser HERMANO real de la vista dentro de la MISMA ventana
    top-level (dos ventanas top-level separadas, activándose una a la
    otra, reintroducen un comportamiento propio de Qt —al reactivar una
    ventana, restaura el foco "recordado" de esa ventana— que no tiene
    nada que ver con el bug real y da falsos positivos/negativos). Por
    eso esta vista nace parentada en un contenedor propio desde el
    arranque, nunca fue top-level independiente, y no se comparte con
    OpenTitleMarqueeTest — vale la instancia propia de libVLC acá."""

    def setUp(self):
        self.container = QWidget()
        layout = QVBoxLayout(self.container)
        self.decoy = QPushButton()
        self.view = tv_player.TVPlayerView()
        layout.addWidget(self.decoy)
        layout.addWidget(self.view)
        self.container.resize(1280, 900)
        self.container.show()
        app.processEvents()

    def tearDown(self):
        self.container.hide()
        self.view.media_player.stop()
        self.view.media_player.release()
        self.view._engine.vlc_instance.release()
        while QApplication.overrideCursor() is not None:
            QApplication.restoreOverrideCursor()

    def test_abrir_viniendo_de_foco_afuera_de_la_vista_no_muestra_el_overlay_completo(self):
        """Bug real reportado en vivo: sacar la llamada explícita a
        _restart_hide_timer() de play_url() no alcanzaba por sí solo —
        el propio self.video_frame.setFocus() de ahí (necesario para que
        espacio/flechas funcionen apenas arranca a reproducir) disparaba
        _on_focus_changed() por la vía genérica de
        QApplication.focusChanged, que interpretaba "el foco entró a la
        vista" como actividad real y reabría el overlay completo igual.
        Fix: ese setFocus() de play_url() ahora está envuelto en
        self._auto_hiding, mismo truco que ya usaba _hide_controls()
        para el mismo problema."""
        self.view._hide_controls()
        self.decoy.setFocus()
        app.processEvents()
        self.assertIs(QApplication.focusWidget(), self.decoy)  # sanity de la precondición

        with patch("tv_player.progress_store.get_progress_ms", return_value=None), \
             patch.object(self.view._engine, "play_url"), \
             patch.object(self.view.media_player, "video_set_marquee_string"), \
             patch.object(self.view.media_player, "video_set_marquee_int"):
            self.view.play_url(
                "", title="Episodio 01", library_id="lib1", subpath=("ep.mkv",), library="lib1-fake",
            )

        self.assertFalse(
            self.view.control_bar_widget.isVisible(),
            "abrir un video viniendo de foco afuera de la vista no debería mostrar el overlay completo",
        )


if __name__ == "__main__":
    unittest.main()
