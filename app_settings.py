"""
Preferencias generales de la app, locales a esta máquina (no por
biblioteca, a diferencia de progress_store/track_prefs que sí son por
biblioteca+serie). Por ahora solo el título de la colección que se
muestra en la esquina superior izquierda de la galería, al lado del logo
(default "Biblioteca").
"""

import json
import os
import shutil
import threading
import uuid
from pathlib import Path

# Ver nota en library_store.py: respeta XDG_CONFIG_HOME (necesario dentro
# del sandbox Flatpak, Paso 21) en vez de asumir siempre $HOME/.config.
_XDG_CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
CONFIG_DIR = _XDG_CONFIG_HOME / "lunyx"
SETTINGS_FILE = CONFIG_DIR / "settings.json"

# Migración desde el nombre viejo del proyecto (Paso 22) — ver
# library_store.py, mismo criterio: copiar si hace falta, nunca borrar.
_OLD_SETTINGS_FILE = _XDG_CONFIG_HOME / "anime-vlc-client" / "settings.json"
if not SETTINGS_FILE.exists() and _OLD_SETTINGS_FILE.exists():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy(_OLD_SETTINGS_FILE, SETTINGS_FILE)

DEFAULT_COLLECTION_TITLE = "Biblioteca"
# "desktop" o "tv" — ver app_restart.py. Persiste entre arranques: una vez
# que se cambia a "tv", la app sigue abriendo en modo TV hasta que se
# vuelva a cambiar (con el mismo toggle, ahora en sentido inverso).
DEFAULT_UI_MODE = "desktop"

# Mismo lock que library_metadata._uid_lock, mismo motivo: get_device_id()
# se puede llamar desde varios QThread a la vez (ScanWorker +
# ScanEpisodesWorker arrancando casi juntos al abrir la app, cada uno
# necesitando el id de dispositivo para nombrar su archivo remoto de
# progreso centralizado, ver centralized_store.py) — sin el lock, dos
# threads podrían leer "todavía no hay device_id" a la vez, generar cada
# uno el suyo, y guardar los dos (el último gana, pisando al primero);
# cada thread devolvería a su caller el id que generó él, no el que
# terminó persistido. El lock serializa "leer + ¿ya existe? + generar +
# guardar" — el segundo thread que llega, tras esperar al primero,
# encuentra el id YA guardado y lo devuelve tal cual.
_device_id_lock = threading.Lock()


def _load():
    if not SETTINGS_FILE.exists():
        return {}
    with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(data):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def get_collection_title():
    return _load().get("collection_title") or DEFAULT_COLLECTION_TITLE


def set_collection_title(title):
    data = _load()
    title = (title or "").strip()
    if title:
        data["collection_title"] = title
    else:
        data.pop("collection_title", None)
    _save(data)


def get_ui_mode():
    return _load().get("ui_mode") or DEFAULT_UI_MODE


def set_ui_mode(mode):
    data = _load()
    data["ui_mode"] = mode
    _save(data)


def get_device_id():
    """Identidad ESTABLE de esta instalación (no de una biblioteca — a
    diferencia de library_metadata.get_or_create_library_uid, que es un
    id COMPARTIDO entre instalaciones que apuntan al mismo share, este es
    justo lo opuesto: único por máquina/instalación). Usado para nombrar
    el archivo remoto propio de progreso centralizado
    (progress.<device_id>.json, ver centralized_store.py) — cada
    dispositivo escribe SOLO el suyo, nunca el de otro, así que dos
    equipos nunca pueden pisarse el mismo archivo.

    Generado la primera vez que hace falta (lazy) y persistido en
    settings.json — 12 hex (uuid4 truncado, 48 bits) alcanza de sobra
    para distinguir un puñado de instalaciones personales, y sin guiones
    da un nombre de archivo simple sin caracteres para escapar en SMB."""
    with _device_id_lock:
        data = _load()
        device_id = data.get("device_id")
        if device_id:
            return device_id
        device_id = uuid.uuid4().hex[:12]
        data["device_id"] = device_id
        _save(data)
        return device_id
