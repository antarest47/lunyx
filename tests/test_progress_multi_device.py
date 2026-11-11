"""
Prueba de punta a punta del rediseño "un archivo por dispositivo" (ver
CONTEXT.md Paso 38+ y centralized_store.py): dos dispositivos reales —
dos `RemoteJsonStore`, cada uno con su propio archivo local y su propio
`device_id` — compartiendo el MISMO target remoto (una carpeta local,
sin SMB real, mismo patrón que tests/test_movie_sort_order.py),
ejercitando el código real de punta a punta: I/O de archivos real,
QThread real para los pushes. Lo único fijado a mano es el `device_id`
(en la app real sale de app_settings.get_device_id(); acá se parchea
para poder controlar determinísticamente "cuál dispositivo es cuál"
dentro de un solo proceso — más rápido y preciso que dos subprocesos
reales para afirmar cosas como "no bloqueó" o "llegó dentro del tiempo",
ver CONTEXT.md).

Es la prueba que el bug original NO PODÍA pasar bajo el diseño viejo: ahí
cada guardado remoto subía el documento COMPARTIDO entero, así que dos
dispositivos guardando series distintas se pisaban por construcción — acá
cada uno escribe solo el suyo, nunca hay dos escritores del mismo
archivo.
"""

import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

import centralized_store
import library_store


def _as_device(device_id):
    """Dentro de este `with`, cualquier RemoteJsonStore que resuelva su
    propio device_id (push/preload/get) se comporta como si fuera esa
    instalación puntual — ver módulo docstring."""
    return patch("centralized_store.app_settings.get_device_id", return_value=device_id)


class TwoDevicesTest(unittest.TestCase):
    def setUp(self):
        self._tmpdirs = []
        self.target_dir = self._mkdtemp("target")
        self.library = library_store.Library.new(
            name="Compartida", type_="series", server="", share="",
            path=self._mkdtemp("lib"), username="", connection_type="local",
            progress_centralized=True, progress_connection_type="local",
            progress_path=self.target_dir,
        )
        self.store_a = self._new_store("local-a")
        self.store_b = self._new_store("local-b")

    def tearDown(self):
        for path in self._tmpdirs:
            shutil.rmtree(path, ignore_errors=True)

    def _mkdtemp(self, label):
        path = tempfile.mkdtemp(prefix=f"lunyx-test-{label}-")
        self._tmpdirs.append(path)
        return path

    def _new_store(self, label):
        return centralized_store.RemoteJsonStore(Path(self._mkdtemp(label)) / "progress.json", "progress")

    def test_dos_dispositivos_series_distintas_no_se_pisan(self):
        """El bug original: A guarda y sube Serie A, B guarda y sube
        Serie B SIN haber leído a A — las dos tienen que sobrevivir."""
        with _as_device("aaaaaaaaaaaa"):
            self.store_a.set_entry("SerieA/ep01.mkv", {"position_ms": 60_000, "duration_ms": 1_000_000})
            self.store_a.push(self.library, blocking=True)

        with _as_device("bbbbbbbbbbbb"):
            self.store_b.set_entry("SerieB/ep01.mkv", {"position_ms": 120_000, "duration_ms": 1_000_000})
            self.store_b.push(self.library, blocking=True)

        remote_files = sorted(p.name for p in Path(self.target_dir).iterdir())
        self.assertEqual(len(remote_files), 2, f"esperaba 2 archivos remotos, uno por dispositivo: {remote_files}")

        with _as_device("aaaaaaaaaaaa"):
            self.store_a.preload(self.library)
            merged = self.store_a.get(self.library)

        self.assertEqual(merged["SerieA/ep01.mkv"]["position_ms"], 60_000, "A no debería perder lo suyo")
        self.assertEqual(merged["SerieB/ep01.mkv"]["position_ms"], 120_000, "A tiene que ver lo que subió B")

    def test_equipo_en_pausa_reescribiendo_no_borra_lo_de_otro(self):
        """El escenario real reportado por el usuario: un equipo con el
        reproductor pausado sigue guardando LOCAL cada 5s (eso no cambió),
        pero con este diseño el push remoto ya no es un timer que corre
        solo — reescribir la misma entrada muchas veces localmente no
        genera pushes de más, y ninguno de esos pushes puede pisar lo que
        guardó otro dispositivo mientras tanto."""
        with _as_device("bbbbbbbbbbbb"):
            self.store_b.set_entry("SerieB/ep02.mkv", {"position_ms": 300_000, "duration_ms": 1_000_000})
            self.store_b.push(self.library, blocking=True)
            # simula el timer de guardado LOCAL de siempre, con el video pausado
            for i in range(20):
                self.store_b.set_entry("SerieB/ep02.mkv", {"position_ms": 300_000, "duration_ms": 1_000_000})

        with _as_device("aaaaaaaaaaaa"):
            self.store_a.set_entry("SerieA/ep01.mkv", {"position_ms": 90_000, "duration_ms": 1_000_000})
            self.store_a.push(self.library, blocking=True)

        with _as_device("bbbbbbbbbbbb"):
            self.store_b.preload(self.library)
            merged = self.store_b.get(self.library)
        self.assertIn("SerieA/ep01.mkv", merged, "lo que guardó A no debería desaparecer")
        self.assertIn("SerieB/ep02.mkv", merged)

    def test_push_no_bloqueante_es_instantaneo_y_llega_al_otro_dispositivo(self):
        """Pausa = async, tiene que sentirse instantáneo (ver
        main.py/tv_player.py PlayerView._toggle_play_pause)."""
        with _as_device("aaaaaaaaaaaa"):
            self.store_a.set_entry("SerieA/ep01.mkv", {"position_ms": 1_000, "duration_ms": 1_000_000})
            t0 = time.monotonic()
            self.store_a.push(self.library, blocking=False)
            elapsed = time.monotonic() - t0
            self.assertLess(elapsed, 0.05, "no debería bloquear")
            self.store_a.wait_pending(self.library, timeout_ms=3000)  # solo para poder afirmar lo de abajo

        with _as_device("bbbbbbbbbbbb"):
            self.store_b.preload(self.library)
            self.assertIn("SerieA/ep01.mkv", self.store_b.get(self.library))

    def test_volver_bloqueante_espera_el_push_de_verdad(self):
        """"Volver" (blocking=True) tiene que garantizar que lo guardado
        ya está en el share antes de devolver el control — a diferencia
        de la pausa, acá sí hace falta la garantía."""
        with _as_device("aaaaaaaaaaaa"):
            self.store_a.set_entry("SerieA/ep03.mkv", {"position_ms": 5_000, "duration_ms": 1_000_000})
            self.store_a.push(self.library, blocking=True)
            # Sin esperar nada más: si push(blocking=True) hizo su trabajo,
            # el archivo YA tiene que estar en disco en este punto.
            remote_files = list(Path(self.target_dir).iterdir())
            self.assertEqual(len(remote_files), 1)
            with open(remote_files[0]) as f:
                content = f.read()
            self.assertIn("SerieA/ep03.mkv", content)

    def test_wait_pending_no_espera_el_timeout_completo_si_ya_termino(self):
        """Regresión específica (bug real encontrado en vivo, Paso 45):
        wait_pending() dependía de que la señal `finished` (conexión
        QUEUED) ya hubiera disparado la limpieza interna para darse
        cuenta de que un push terminó — pero esa entrega nunca puede
        pasar mientras el propio hilo que espera sigue bloqueado adentro
        de wait_pending() (es el caso real: el hilo de Qt, llamando
        desde "Volver"). Resultado real medido: CADA "Volver" quemaba el
        timeout ENTERO (3s) en un busy-loop, aunque el archivo ya
        estuviera escrito en disco hacía rato. Este test pone un timeout
        generoso (10s) y exige que vuelva MUCHO antes — si el bug
        reaparece, tarda ~10s en vez de milisegundos."""
        with _as_device("aaaaaaaaaaaa"):
            self.store_a.set_entry("SerieA/ep04.mkv", {"position_ms": 1_000, "duration_ms": 1_000_000})
            t0 = time.monotonic()
            self.store_a.push(self.library, blocking=True, timeout_ms=10_000)
            elapsed = time.monotonic() - t0
        self.assertLess(
            elapsed, 1.0,
            f"push(blocking=True) tardó {elapsed:.2f}s con un tope de 10s — "
            "señal de que volvió el busy-loop del bug viejo",
        )


if __name__ == "__main__":
    unittest.main()
