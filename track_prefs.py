"""
Preferencia de pista de audio/subtítulo por defecto, por serie (no por
episodio individual) — botón "Usar para toda la serie" en el player. Por
defecto vive en ~/.config/lunyx/track_prefs.json; puede centralizarse
igual que progress_store.py (ver centralized_store.py, diseño "un
archivo por dispositivo").

No se puede guardar por track_id numérico: cada archivo puede tener sus
pistas en un orden distinto. Se guarda por el TEXTO de la descripción de
la pista (ej. "Japanese", "Signs/Songs"), y al cargar un episodio nuevo se
busca la pista cuya descripción contenga ese texto (case-insensitive). Si
no hay match, se deja lo que VLC elija por defecto.
"""

import os
import re
import shutil
from pathlib import Path

import centralized_store

# Ver nota en library_store.py: respeta XDG_CONFIG_HOME (necesario dentro
# del sandbox Flatpak, Paso 21) en vez de asumir siempre $HOME/.config.
_XDG_CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
CONFIG_DIR = _XDG_CONFIG_HOME / "lunyx"
PREFS_FILE = CONFIG_DIR / "track_prefs.json"

# Migración desde el nombre viejo del proyecto (Paso 22) — ver
# library_store.py, mismo criterio: copiar si hace falta, nunca borrar.
_OLD_PREFS_FILE = _XDG_CONFIG_HOME / "anime-vlc-client" / "track_prefs.json"
if not PREFS_FILE.exists() and _OLD_PREFS_FILE.exists():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy(_OLD_PREFS_FILE, PREFS_FILE)

_store = centralized_store.RemoteJsonStore(PREFS_FILE, "track_prefs")


def _key(prefix, series_subpath_root):
    return f"{prefix}|{series_subpath_root}"


def get_prefs(library, series_subpath_root):
    prefix = centralized_store.key_prefix(library)
    if prefix is None:
        return {}
    return _store.get(library).get(_key(prefix, series_subpath_root), {})


def set_audio_pref(library, series_subpath_root, description):
    prefix = centralized_store.key_prefix(library)
    if prefix is None:
        return
    key = _key(prefix, series_subpath_root)
    # Se guarda audio Y subtitle bajo la MISMA entrada — hay que
    # preservar lo que ya haya del otro campo (get() ya trae el merge
    # fusionado, incluido lo que este mismo dispositivo guardó antes).
    # set_entry() pisa "updated_at" con uno fresco sin que haga falta
    # sacarlo a mano del dict que ya vino con uno viejo.
    current = _store.get(library).get(key, {})
    _store.set_entry(key, {**current, "audio": description})
    _store.push(library, blocking=False)


def set_subtitle_pref(library, series_subpath_root, description):
    prefix = centralized_store.key_prefix(library)
    if prefix is None:
        return
    key = _key(prefix, series_subpath_root)
    current = _store.get(library).get(key, {})
    _store.set_entry(key, {**current, "subtitle": description})
    _store.push(library, blocking=False)


def delete_library_entries(library):
    """Ver progress_store.delete_library_entries — mismo criterio, misma
    razón (no dejar preferencias de pista huérfanas al borrar una
    biblioteca), y misma corrección: SIEMPRE local, sin tombstone, nunca
    toca un target remoto centralizado. Purga los dos prefijos posibles
    (library.id y el uid compartido si se pudo resolver)."""
    _store.delete_prefix_local_only(f"{library.id}|")
    uid_prefix = centralized_store.key_prefix(library)
    if uid_prefix is not None:
        _store.delete_prefix_local_only(f"{uid_prefix}|")


# libVLC arma la descripción de cada pista como "<nombre> - [<idioma>]",
# con el nombre de idioma TRADUCIDO según el locale de la sesión que
# consultó la descripción (ver CONTEXT.md: bug real, "Inglés" vs
# "English" según qué máquina/sesión sondeó la serie primero). El texto
# guardado como preferencia es justamente esa descripción completa, con
# el corchete ya "congelado" en el idioma de cuando se guardó — si
# después se reproduce bajo un locale distinto, ese corchete final ya no
# coincide con el que da la descripción EN VIVO aunque sea la misma
# pista. Se recorta antes de reintentar el match porque es la única
# parte de la cadena que varía con el locale.
_LANGUAGE_SUFFIX_RE = re.compile(r"\s*-\s*\[[^\[\]]*\]\s*$")


def _strip_language_suffix(text):
    return _LANGUAGE_SUFFIX_RE.sub("", text)


def find_matching_track(descriptions, preferred_text):
    """descriptions: lista de (track_id, description) de libVLC. Devuelve
    el track_id cuya descripción contiene preferred_text (sin importar
    mayúsculas), o None si no hay match.

    Dos pasadas: primero el texto completo tal cual se guardó (match más
    preciso, funciona si el locale no cambió desde que se guardó);
    si no matchea nada, se reintenta sin el corchete final de idioma —
    ver _strip_language_suffix."""
    if not preferred_text:
        return None
    decoded = []
    for track_id, description in descriptions or []:
        text = description.decode("utf-8", errors="replace") if isinstance(description, bytes) else str(description)
        decoded.append((track_id, text))

    needle = preferred_text.lower()
    for track_id, text in decoded:
        if needle in text.lower():
            return track_id

    needle = _strip_language_suffix(preferred_text).lower()
    if not needle:
        return None
    for track_id, text in decoded:
        if needle in _strip_language_suffix(text).lower():
            return track_id
    return None


def preload(library):
    """Ver centralized_store.RemoteJsonStore.preload — llamar SOLO desde
    un QThread que ya está corriendo (ScanWorker/ScanEpisodesWorker)."""
    _store.preload(library)


def refresh(library):
    """Ver progress_store.refresh — mismo criterio. Solo desde QThread."""
    _store.invalidate(library)
    _store.preload(library)


def checkpoint(library, blocking=False, timeout_ms=3000):
    """Ver progress_store.checkpoint — mismo criterio. set_audio_pref/
    set_subtitle_pref ya disparan un push async por su cuenta; esto queda
    público sobre todo para el flujo de migración (gallery.py
    ProgressMigrationApplyWorker), que sí quiere esperar a que la subida
    termine antes de cerrar el diálogo."""
    _store.push(library, blocking=blocking, timeout_ms=timeout_ms)


def wait_pending(library, timeout_ms=3000):
    _store.wait_pending(library, timeout_ms)


def wait_all_pending(timeout_ms=3000):
    _store.wait_all_pending(timeout_ms)


def analyze_migration(library):
    return centralized_store.analyze_migration_for_store(_store, f"{library.id}|")


def apply_migration(library, remote_prefix, migrate):
    centralized_store.apply_migration_for_store(_store, f"{library.id}|", f"{remote_prefix}|", migrate)
