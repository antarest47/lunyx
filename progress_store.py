"""
Progreso de reproducción por episodio/película. Por defecto vive en
~/.config/lunyx/progress.json (análogo a libraries.json); una biblioteca
puede opcionalmente centralizarlo en un share/carpeta propio (ver
Library.progress_* en library_store.py y centralized_store.py, que hace
toda la mecánica de cache/threading para que esto nunca bloquee la UI).

La clave es library_id + subpath (join con "/"), que identifica un
archivo de forma estable sin depender de índices ni de rutas absolutas.

Guardar y sincronizar son dos pasos SEPARADOS a propósito (diseño "un
archivo por dispositivo", ver centralized_store.py): save_progress()
SIEMPRE es local, instantáneo, nunca bloquea ni dispara red. Subir al
share remoto es checkpoint(), disparado explícitamente en los momentos
que importan (ver main.py/tv_player.py): pausa (async, sin bloquear —
pausar tiene que sentirse instantáneo) y "Volver"/cierre de la app
(bloqueante con tope, para darle al último guardado una chance real de
llegar antes de que la UI avance).
"""

import os
import shutil
import time
from pathlib import Path

import centralized_store

# Ver nota en library_store.py: respeta XDG_CONFIG_HOME (necesario dentro
# del sandbox Flatpak, Paso 21) en vez de asumir siempre $HOME/.config.
_XDG_CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
CONFIG_DIR = _XDG_CONFIG_HOME / "lunyx"
PROGRESS_FILE = CONFIG_DIR / "progress.json"

# Migración desde el nombre viejo del proyecto (Paso 22) — ver
# library_store.py, mismo criterio: copiar si hace falta, nunca borrar.
_OLD_PROGRESS_FILE = _XDG_CONFIG_HOME / "anime-vlc-client" / "progress.json"
if not PROGRESS_FILE.exists() and _OLD_PROGRESS_FILE.exists():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy(_OLD_PROGRESS_FILE, PROGRESS_FILE)

# A partir de este % de avance se considera "visto" y se borra el
# progreso guardado, para no quedar reanudando siempre a 2 segundos del
# final (mismo criterio que usan Plex/Jellyfin, aprox.).
FINISHED_THRESHOLD = 0.95

_store = centralized_store.RemoteJsonStore(PROGRESS_FILE, "progress")


def _key(prefix, subpath):
    return f"{prefix}|{'/'.join(subpath)}"


def get_progress_ms(library, subpath):
    """Devuelve la posición guardada en ms, o None si no hay progreso
    (nunca se reprodujo, ya se marcó como terminado, o la biblioteca
    centraliza y todavía no se pudo leer ningún archivo remoto)."""
    prefix = centralized_store.key_prefix(library)
    if prefix is None:
        return None
    entry = _store.get(library).get(_key(prefix, subpath))
    return entry["position_ms"] if entry else None


def get_progress_fraction(library, subpath):
    """0.0-1.0, o None si no hay progreso guardado — para la barra fina
    de progreso en la lista de episodios (ver episodes.py EpisodeRow)."""
    prefix = centralized_store.key_prefix(library)
    if prefix is None:
        return None
    entry = _store.get(library).get(_key(prefix, subpath))
    if not entry or not entry.get("duration_ms"):
        return None
    return entry["position_ms"] / entry["duration_ms"]


def get_all_progress(libraries):
    """[(library_id, subpath, position_ms, duration_ms, updated_at), ...]
    — de TODAS las bibliotecas en `libraries` (no solo la seleccionada),
    ordenadas por más reciente primero. Para el estante "Continuar viendo"
    de la galería (gallery.py). Bibliotecas centralizadas cuyo target
    remoto todavía no se precargó esta sesión simplemente no aportan
    entradas todavía (se resuelven solas apenas termine su ScanWorker/
    ScanEpisodesWorker) — mismo criterio que ya usa
    _resolve_continue_watching_entries para series sin escanear.

    El library_id que se DEVUELVE es siempre el id LOCAL de esta máquina,
    aunque la entrada esté guardada bajo el uid compartido (bibliotecas
    centralizadas) — así gallery.py sigue resolviendo contra
    _library_by_id sin enterarse de que existen dos espacios de nombres.
    Entradas cuyo prefijo no corresponde a ninguna biblioteca configurada
    acá (huérfanas, o de otra instalación con otro set de bibliotecas) se
    descartan solas."""
    local_id_by_prefix = {}
    for library in libraries:
        prefix = centralized_store.key_prefix(library)
        if prefix is not None:
            local_id_by_prefix[prefix] = library.id

    result = []
    for key, entry in _store.iter_all_entries(libraries):
        entry_prefix, subpath_str = key.split("|", 1)
        local_id = local_id_by_prefix.get(entry_prefix)
        if local_id is None:
            continue
        result.append((
            local_id, tuple(subpath_str.split("/")),
            entry["position_ms"], entry["duration_ms"], entry.get("updated_at", 0),
        ))
    result.sort(key=lambda item: item[4], reverse=True)
    return result


def delete_library_entries(library):
    """Borra todo el progreso guardado de una biblioteca — se llama al
    eliminar una biblioteca (ver gallery.py AddLibraryDialog._delete_library)
    para no dejar entradas huérfanas apuntando a un library_id que ya no
    existe (causaba que la tab "Continuar viendo" quedara visible pero
    vacía). SIEMPRE local, SIN tombstone (ver
    RemoteJsonStore.delete_prefix_local_only): si la biblioteca
    centralizaba, ese progreso puede seguir siendo válido para otra
    instalación — borrar la biblioteca de ESTA app nunca debe avisarle a
    nadie más que se borró.

    Purga los DOS prefijos posibles: library.id (donde vive si nunca
    centralizó, o si centralizó pero el usuario eligió no migrar lo
    local) y el uid compartido si se pudo resolver (donde vive el
    progreso realmente centralizado)."""
    _store.delete_prefix_local_only(f"{library.id}|")
    uid_prefix = centralized_store.key_prefix(library)
    if uid_prefix is not None:
        _store.delete_prefix_local_only(f"{uid_prefix}|")


def delete_series_entries(library, series_root):
    """Borra el progreso de TODOS los episodios de una serie (no solo el
    actual) — usado por "Marcar serie como vista" (episodes.py, solo
    desktop). Al no quedar ninguna entrada en progreso, la serie
    desaparece sola de "Continuar viendo" — no hace falta un flag de
    "visto" aparte, acá nunca existió un registro de "visto", solo
    ausencia de progreso (ver save_progress: al llegar a
    FINISHED_THRESHOLD directamente se borra la entrada en vez de
    marcarse).

    Tombstone de PREFIJO (ver RemoteJsonStore.delete_prefix): tapa
    entradas de CUALQUIER dispositivo en el próximo merge, incluidos
    episodios que ESTE dispositivo nunca sincronizó — caso real: la
    laptop tiene progreso de un episodio que el escritorio nunca vio;
    si el escritorio marca la serie como vista, ese episodio tiene que
    desaparecer igual. Dispara un push async (mismo criterio que una
    pausa — no bloquear la UI por esto)."""
    key_prefix = centralized_store.key_prefix(library)
    if key_prefix is None:
        return
    prefix = f"{key_prefix}|{series_root}/"
    _store.delete_prefix(prefix)
    _store.push(library, blocking=False)


def save_progress(library, subpath, position_ms, duration_ms):
    """SIEMPRE local, síncrono, nunca dispara push por sí sola — ver
    checkpoint() para cuándo subir al remoto. Llamada tanto por el timer
    periódico (cada 5s mientras se reproduce) como por los checkpoints
    (que guardan el último tramo antes de sincronizar)."""
    if not duration_ms or duration_ms <= 0 or position_ms is None or position_ms < 0:
        return
    prefix = centralized_store.key_prefix(library)
    if prefix is None:
        return  # centraliza pero el uid compartido todavía no se resolvió — mejor no guardar que guardar mal

    key = _key(prefix, subpath)
    if position_ms / duration_ms >= FINISHED_THRESHOLD:
        _store.delete_entry(key)
    else:
        _store.set_entry(key, {"position_ms": position_ms, "duration_ms": duration_ms})


def checkpoint(library, blocking=False, timeout_ms=3000):
    """Sube al target remoto lo que haya guardado local para esta
    biblioteca — no-op si no centraliza. blocking=False (pausa): dispara
    y vuelve al toque. blocking=True ("Volver", cierre de la app):
    dispara (si hace falta) y espera con tope — ver
    RemoteJsonStore.push/wait_pending."""
    _store.push(library, blocking=blocking, timeout_ms=timeout_ms)


def preload(library):
    """Ver centralized_store.RemoteJsonStore.preload — llamar SOLO desde
    un QThread que ya está corriendo (ScanWorker/ScanEpisodesWorker)."""
    _store.preload(library)


def refresh(library):
    """Tira la cache y vuelve a leer del target — para ver el progreso
    guardado desde OTRA máquina. Llamar SOLO desde un QThread."""
    _store.invalidate(library)
    _store.preload(library)


def wait_pending(library, timeout_ms=3000):
    _store.wait_pending(library, timeout_ms)


def wait_all_pending(timeout_ms=3000):
    _store.wait_all_pending(timeout_ms)


def analyze_migration(library):
    return centralized_store.analyze_migration_for_store(_store, f"{library.id}|")


def apply_migration(library, remote_prefix, migrate):
    centralized_store.apply_migration_for_store(_store, f"{library.id}|", f"{remote_prefix}|", migrate)
