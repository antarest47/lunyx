"""
Guarda contra que vuelva a romperse el campo de orden manual de
películas (ver library_metadata.set_movie_sort_key / gallery.ScanWorker /
main.py PlayerView._apply_movie_sort_key).

Caso real que motivó el feature: sagas con numeración romana ("Saga
Movie IV/V/IX") no ordenan bien alfabéticamente por título — "IV" < "IX"
< "V" en orden de string, aunque el orden real sea IV, V, IX. Sin un
sort_key custom, sigue ordenando por título (comportamiento de siempre,
sin regresión para quien no usa el campo).

Corre contra una biblioteca LOCAL en un directorio temporal — no toca
~/.config/lunyx ni ninguna biblioteca real (Library.new() nunca se
persiste con library_store.add_library()).
"""

import os
import shutil
import sys
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

import gallery
import library_metadata
import library_store


class MovieSortOrderTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="lunyx-test-movies-")
        self.library = library_store.Library.new(
            name="Test movies", type_="movies", server="", share="",
            path=self.tmpdir, username="", connection_type="local",
        )
        library_metadata.clear_cache()

    def tearDown(self):
        library_metadata.clear_cache()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _touch(self, filename):
        open(os.path.join(self.tmpdir, filename), "w").close()

    def _scan(self):
        """Corre ScanWorker.run() sincrónico (sin .start(), sin QThread
        real) — mismo truco usado para validar esto en vivo: el método no
        necesita el event loop de Qt para nada, solo hace I/O local y
        emite señales que acá se conectan con conexión directa (mismo
        hilo)."""
        worker = gallery.ScanWorker(self.library)
        result = {}
        worker.finished_ok.connect(lambda items, season_search: result.update(items=items))
        worker.failed.connect(lambda message: result.update(error=message))
        worker.run()
        self.assertNotIn("error", result, result.get("error"))
        return result["items"]

    def test_orden_custom_respeta_numeracion_romana(self):
        self._touch("Saga Movie IV.mp4")
        self._touch("Saga Movie IX.mp4")
        self._touch("Saga Movie V.mp4")

        # Sin corregir: el orden alfabético de por sí queda roto (IV, IX, V).
        items = self._scan()
        broken_order = [e[3] for e in sorted(items, key=lambda e: e[4].lower())]
        self.assertEqual(broken_order, ["Saga Movie IV", "Saga Movie IX", "Saga Movie V"])

        # Con sort_key custom (lo que hace PlayerView._apply_movie_sort_key
        # al usar el campo "Orden" del panel de corrección): el orden pasa
        # a ser el cronológico real.
        library_metadata.set_movie_sort_key(self.library, "", "Saga Movie IV", "saga movie 4")
        library_metadata.set_movie_sort_key(self.library, "", "Saga Movie V", "saga movie 5")
        library_metadata.set_movie_sort_key(self.library, "", "Saga Movie IX", "saga movie 9")
        library_metadata.clear_cache()

        items = self._scan()
        fixed_order = [e[3] for e in sorted(items, key=lambda e: e[4].lower())]
        self.assertEqual(fixed_order, ["Saga Movie IV", "Saga Movie V", "Saga Movie IX"])

    def test_pelicula_sin_sort_key_cae_al_titulo(self):
        """Sin ningún sort_key seteado, el orden sigue siendo el de
        siempre (alfabético por título mostrado) — no debería haber
        ninguna regresión para quien nunca toca el campo nuevo."""
        self._touch("Zeta.mp4")
        items = self._scan()
        zeta = next(e for e in items if e[3] == "Zeta")
        self.assertEqual(zeta[4], "Zeta")  # sort_key == display_name == raw_name acá

    def test_key_de_metadata_usa_el_stem_sin_extension(self):
        """Regresión específica encontrada en vivo (Paso 36): el guardado
        del sort_key/nombre custom de una película tiene que usar el
        MISMO stem sin extensión que usa el escaneo (smb_scanner.stem) —
        no el nombre de archivo completo. Si algún caller vuelve a armar
        la key con subpath[0] a secas, el valor queda guardado bajo una
        key que el próximo escaneo nunca lee, y el override parece
        perderse."""
        self._touch("Alguna Pelicula.mp4")
        library_metadata.set_movie_sort_key(self.library, "", "Alguna Pelicula", "alguna 1")
        library_metadata.clear_cache()
        items = self._scan()
        entry = next(e for e in items if e[3] == "Alguna Pelicula")
        self.assertEqual(entry[4], "alguna 1")


if __name__ == "__main__":
    unittest.main()
