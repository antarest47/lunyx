"""
Reproduce en vivo el bug reportado por el usuario en una instalación
Debian 13 nueva (ver CONTEXT.md): al reproducir un episodio, el open SMB
fallaba con "Invalid argument" (libsmbclient) pero libVLC NUNCA disparaba
MediaPlayerEncounteredError ni pasaba a vlc.State.Error — el demux se
quedaba reintentando decodificar para siempre (spam de "DecodeFrame
failed"/"buffer deadlock prevented" en stderr), y como PlayerEngine no
tenía NINGÚN tope de tiempo para llegar a "Playing", la app entera se
congelaba sin salida ("No responde", hubo que matar el proceso).

Este test no depende de SMB real: reproduce la MISMA clase de falla de
forma determinística y 100% local con un FIFO (os.mkfifo) sin ningún
escritor — abrir un FIFO en modo lectura bloquea en el syscall open() del
sistema operativo, sin ningún evento de libVLC posible (ni Playing ni
Error), igual que el open colgado real. Confirma que PlayerEngine.media_failed
rescata esa apertura vía el watchdog (PLAYBACK_OPEN_TIMEOUT_S) en vez de
quedarse esperando para siempre.
"""

import os
import sys
import tempfile
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

import player_engine

_TEST_TIMEOUT_S = 0.5  # acelera el test; el valor real de producción (20s) se restaura en tearDown
_WAIT_BUDGET_S = 5


class PlaybackOpenWatchdogTest(unittest.TestCase):
    def setUp(self):
        self._original_timeout = player_engine.PLAYBACK_OPEN_TIMEOUT_S
        player_engine.PLAYBACK_OPEN_TIMEOUT_S = _TEST_TIMEOUT_S
        self._fifo_dir = tempfile.mkdtemp()
        self._fifo_path = os.path.join(self._fifo_dir, "stuck_open.mkv")
        os.mkfifo(self._fifo_path)
        self._engine = player_engine.PlayerEngine()

    def tearDown(self):
        player_engine.PLAYBACK_OPEN_TIMEOUT_S = self._original_timeout
        # Desbloquea el thread interno de libVLC que quedó esperando un
        # writer del FIFO (si no, el open() bloqueado sobrevive al test).
        try:
            fd = os.open(self._fifo_path, os.O_WRONLY | os.O_NONBLOCK)
            os.close(fd)
        except OSError:
            pass
        self._engine.media_player.stop()
        self._engine.media_player.release()
        self._engine.vlc_instance.release()
        os.remove(self._fifo_path)
        os.rmdir(self._fifo_dir)

    def test_stuck_open_without_any_vlc_event_recovers_via_watchdog(self):
        received = []
        self._engine.media_failed.connect(lambda message: received.append(message))

        self._engine.play_url(self._fifo_path, [], 0)

        deadline = time.monotonic() + _WAIT_BUDGET_S
        while not received and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.05)

        self.assertTrue(
            received,
            "media_failed nunca se emitió — una apertura colgada (sin evento de "
            "libVLC) se hubiera quedado esperando para siempre, como el bug real.",
        )


if __name__ == "__main__":
    unittest.main()
