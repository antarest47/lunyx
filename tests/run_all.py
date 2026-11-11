"""
Corre toda la carpeta tests/ — Qt real (offscreen), sin frameworks
nuevos, mismo estilo que ya se usa para validar cosas en vivo durante el
desarrollo (ver CONTEXT.md), solo que estos quedan guardados en vez de
tirarse al terminar de confirmar un fix.

Uso:
    venv/bin/python tests/run_all.py
    venv/bin/python tests/run_all.py -v          # nombre de cada test
    venv/bin/python -m unittest tests.test_movie_sort_order   # uno solo

Cada archivo test_*.py también corre solo: venv/bin/python tests/test_x.py
"""

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent

if __name__ == "__main__":
    extra_args = sys.argv[1:] or ["-v"]
    result = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", *extra_args],
        cwd=REPO_ROOT,
    )
    sys.exit(result.returncode)
