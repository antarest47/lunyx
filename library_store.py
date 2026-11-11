"""
Persistencia de bibliotecas configuradas por el usuario.

Cada biblioteca tiene un tipo de contenido ("series" o "movies") y un
"connection_type" que decide cómo se accede a ella (ver smb_scanner.py,
que abstrae la diferencia detrás de las mismas funciones):
- "smb": share SMB (server + share + subpath opcional dentro del share).
- "local": carpeta del disco de esta máquina — "path" es la ruta
  absoluta local en vez de un subpath dentro de un share, y
  server/share/username no aplican (quedan vacíos).

Los datos de conexión (menos la contraseña SMB, que va a keyring/KDE
Wallet vía credentials.py — no aplica a bibliotecas locales, no hay
credenciales que guardar) se guardan en un JSON simple en el directorio
de config del usuario.
"""

import json
import os
import shutil
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

# os.environ["XDG_CONFIG_HOME"], no Path.home() / ".config" a secas: dentro
# de un sandbox Flatpak, $HOME apunta a "/home/<user>" igual que afuera pero
# NO está bindeado a nada real ahí — Flatpak redirige la config vía la env
# var XDG_CONFIG_HOME (a ~/.var/app/<id>/config/), no vía el path literal
# $HOME/.config. Ignorar la env var hacía que la app nunca viera su propio
# libraries.json corriendo empaquetada (Paso 21).
_XDG_CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
CONFIG_DIR = _XDG_CONFIG_HOME / "lunyx"
LIBRARIES_FILE = CONFIG_DIR / "libraries.json"

# Migración desde el nombre viejo del proyecto ("anime-vlc-client", Paso
# 22) — si ya existe el archivo con el nombre nuevo no se toca nada; si
# no existe pero el viejo sí, se copia (el viejo se deja como está, nunca
# se borra — no hay necesidad de arriesgar nada acá).
_OLD_LIBRARIES_FILE = _XDG_CONFIG_HOME / "anime-vlc-client" / "libraries.json"
if not LIBRARIES_FILE.exists() and _OLD_LIBRARIES_FILE.exists():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy(_OLD_LIBRARIES_FILE, LIBRARIES_FILE)

LIBRARY_TYPES = ("series", "movies")
CONNECTION_TYPES = ("smb", "local")


@dataclass
class Library:
    id: str
    name: str
    type: str  # "series" o "movies"
    server: str = ""
    share: str = ""
    path: str = ""  # SMB: subpath dentro del share ("" = raíz). Local: ruta absoluta de la carpeta.
    username: str = ""
    # Default "smb" a propósito: así las bibliotecas ya guardadas antes de
    # que existiera este campo (JSON sin la key) siguen andando igual sin
    # necesidad de migrar nada — Library(**entry) completa el default solo.
    connection_type: str = "smb"
    # progress_* — dónde viven progress.json/track_prefs.json de ESTA
    # biblioteca (ver centralized_store.py). Default False/"local"+vacío a
    # propósito, mismo truco que connection_type: bibliotecas guardadas
    # antes de que existiera este campo siguen usando el store local de
    # siempre sin necesidad de migrar el JSON. La conexión de progreso es
    # independiente de la conexión de la biblioteca (puede apuntar a un
    # server/share distinto) — su contraseña NO se guarda acá, va a
    # keyring bajo su propio service-key (ver centralized_store.progress_password).
    progress_centralized: bool = False
    progress_connection_type: str = "local"  # "smb" o "local", solo relevante si progress_centralized
    progress_server: str = ""
    progress_share: str = ""
    progress_path: str = ""
    progress_username: str = ""

    @staticmethod
    def new(name, type_, server, share, path, username, connection_type="smb",
            progress_centralized=False, progress_connection_type="local",
            progress_server="", progress_share="", progress_path="", progress_username=""):
        return Library(
            id=str(uuid.uuid4()),
            name=name,
            type=type_,
            server=server,
            share=share,
            path=path,
            username=username,
            connection_type=connection_type,
            progress_centralized=progress_centralized,
            progress_connection_type=progress_connection_type,
            progress_server=progress_server,
            progress_share=progress_share,
            progress_path=progress_path,
            progress_username=progress_username,
        )


def _load_raw():
    if not LIBRARIES_FILE.exists():
        return []
    with open(LIBRARIES_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_raw(entries):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(LIBRARIES_FILE, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2, ensure_ascii=False)


def list_libraries():
    return [Library(**entry) for entry in _load_raw()]


def add_library(library):
    entries = _load_raw()
    entries.append(asdict(library))
    _save_raw(entries)


def update_library(library):
    entries = [asdict(library) if e["id"] == library.id else e for e in _load_raw()]
    _save_raw(entries)


def remove_library(library_id):
    entries = [e for e in _load_raw() if e["id"] != library_id]
    _save_raw(entries)
