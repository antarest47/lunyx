"""
Nombres personalizados de serie/temporada. A diferencia de progress_store
y track_prefs (preferencias LOCALES de esta máquina), esto vive junto a
la biblioteca misma — un solo JSON por biblioteca, en el share SMB o en
la carpeta local según corresponda (ver smb_scanner.open_text/register)
— para que sea portable entre instalaciones: si se corre la app desde
otra PC apuntando al mismo share/carpeta, los nombres ya están ahí, no
hay que recrearlos a mano.
"""

import errno
import json
import threading
import time
import uuid

import smb_scanner

METADATA_FILENAME = "lunyx-metadata.json"
# Nombre viejo del proyecto (Paso 22) — bibliotecas reales ya tienen este
# archivo con nombres custom puestos. Se lee como fallback si no existe
# todavía el de nombre nuevo, pero nunca se borra ni se sobreescribe: la
# migración real pasa sola la próxima vez que se guarde algo (_save()
# siempre escribe con METADATA_FILENAME), este archivo queda de respaldo.
_OLD_METADATA_FILENAME = "anime-vlc-client-metadata.json"
# Ver _read_json — reintentos ante JSON ilegible (posible lectura a medio
# camino de una escritura real, ver CONTEXT.md Paso 34).
_READ_RETRY_ATTEMPTS = 3
_READ_RETRY_DELAY_S = 0.15

_cache = {}  # library.id -> dict, cache en memoria de la sesión actual (se invalida con clear_cache)
_uid_lock = threading.Lock()  # ver get_or_create_library_uid


def _read_json(library, filename):
    """None si `filename` genuinamente no existe todavía (o está
    corrupto/vacío) — se relanza como StorageError (mismo criterio que
    centralized_store.RemoteJsonStore._read_device_envelope, mismo bug
    real encontrado ahí: ver esa función para el análisis completo) para
    CUALQUIER OTRO error de lectura, en vez de tragárselo como si el
    archivo estuviera vacío.

    Antes acá había un `except Exception` amplio en `_load()` — un hipo
    de red SMB transitorio se trataba igual que "este archivo nunca
    existió", y ese resultado vacío quedaba CACHEADO EN `_cache` PARA
    SIEMPRE (a diferencia de RemoteJsonStore, que sí distingue
    "ready"/"unavailable" — acá no había ningún estado intermedio). Un
    `set_series_name()`/`set_track_probe()` posterior escribía sobre esa
    base vacía, pisando de verdad nombres/uid/sondeos ya guardados.
    Síntoma real: "Actualizar biblioteca" corría (el escaneo de ítems
    andaba bien, usa smb_scanner.scan_series directo) pero el sondeo de
    pistas nunca aparecía — cada intento de leer el sidecar para chequear
    si una serie ya tenía sondeo (`get_track_probe`) pisaba en silencio
    lo que hubiera, y quedaba atascado en ese estado vacío el resto de la
    sesión (clear_cache() es la única forma de reintentar, y "Actualizar
    biblioteca" sí la llama — pero si el hipo de red vuelve a pasar en
    ESE reintento, se repite el mismo problema).

    `json.JSONDecodeError` (archivo presente pero ilegible) se reintenta
    unas pocas veces antes de tratarlo como "no existe" — la escritura
    NO es atómica (se probó vía rename/replace y falla contra el
    servidor real del usuario, ver centralized_store._write_remote /
    CONTEXT.md Paso 34), así que un archivo mal formado puede ser una
    lectura que agarró una escritura real a medio camino, no
    necesariamente corrupción de verdad."""
    for attempt in range(_READ_RETRY_ATTEMPTS):
        try:
            with smb_scanner.open_text(library, filename, mode="r") as f:
                return json.load(f)
        except FileNotFoundError:
            return None
        except OSError as exc:
            if getattr(exc, "errno", None) == errno.ENOENT:
                return None
            raise smb_scanner.StorageError(f"No se pudo leer {filename}: {exc}") from exc
        except json.JSONDecodeError:
            if attempt < _READ_RETRY_ATTEMPTS - 1:
                time.sleep(_READ_RETRY_DELAY_S)
    return None


def _load(library, password):
    if library.id in _cache:
        return _cache[library.id]
    smb_scanner.register(library, password)
    data = _read_json(library, METADATA_FILENAME)
    if data is None:
        data = _read_json(library, _OLD_METADATA_FILENAME)
    if data is None:
        data = {"series": {}}
    _cache[library.id] = data
    return data


def _save(library, password, data):
    # NO escritura "atómica" vía rename — revertido, ver
    # centralized_store._write_remote (mismo motivo: smbclient.replace()
    # falla contra el servidor real del usuario). Mitigación del lado de
    # la lectura, ver _read_json.
    smb_scanner.register(library, password)
    with smb_scanner.open_text(library, METADATA_FILENAME, mode="w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    _cache[library.id] = data


def clear_cache(library_id=None):
    if library_id is None:
        _cache.clear()
    else:
        _cache.pop(library_id, None)


def get_or_create_library_uid(library, password):
    """Identidad ESTABLE de la biblioteca, compartida entre instalaciones.

    El `Library.id` local NO sirve para esto: `Library.new()` genera un
    uuid4 nuevo en cada máquina (y de nuevo si borrás y reagregás la misma
    carpeta), así que dos instalaciones apuntando al MISMO share tienen
    ids distintos. Para que el progreso centralizado se pueda continuar
    entre la laptop, el escritorio y el HTPC, la clave de cada entrada
    tiene que colgar de algo que viva junto a la biblioteca, no de la
    config local — y este sidecar ya es exactamente eso desde el Paso 11.

    Se genera UNA sola vez (la primera instalación que lo pida) y después
    todas leen el mismo valor. Deliberadamente NO se deriva de
    server/share/path: la misma biblioteca puede montarse distinto en cada
    máquina (SMB en la laptop, carpeta local en el HTPC) y aun así tiene
    que resolver al mismo uid.

    Bug real encontrado en vivo (biblioteca recién agregada, progreso
    centralizado): esta función se llama desde VARIOS QThread distintos
    (ScanWorker del prescan de fondo, ScanEpisodesWorker al abrir cada
    serie) que pueden pisarse — sin el lock, dos threads podían leer el
    sidecar ANTES de que existiera "library_uid", generar cada uno su
    propio uuid4 (distinto), y guardar los dos (el último gana, pisando al
    primero). Cada thread devolvía a su caller el uid que ÉL generó, no el
    que terminó persistido — resolve_library_uid (centralized_store.py)
    cachea ese valor por el resto de la sesión, así que el progreso
    guardado antes de que un thread "perdedor" quedara con el uid
    equivocado terminaba en la clave `{uid_perdedor}|...` del JSON remoto
    — real pero INALCANZABLE para el resto de la sesión, que solo
    consulta `{uid_ganador}|...`. Síntoma real: "Continuar viendo" (y el
    progreso por episodio en la vista de la serie) solo mostraba la
    ÚLTIMA serie mirada — las anteriores, mientras el uid todavía no se
    había asentado, quedaban huérfanas sin que se perdiera el archivo ni
    hubiera ningún error visible. El lock serializa todo el
    "leer+¿ya existe?+generar+guardar": el segundo thread que lo pide,
    tras esperar al primero, encuentra el uid YA guardado en `_load()`
    (que a su vez cachea en `_cache`) y lo devuelve tal cual, sin generar
    uno nuevo — los dos threads terminan de acuerdo."""
    with _uid_lock:
        data = _load(library, password)
        uid = data.get("library_uid")
        if uid:
            return uid
        uid = str(uuid.uuid4())
        data["library_uid"] = uid
        _save(library, password, data)
        return uid


def get_raw_series_name(library, password, series_key):
    """El nombre custom guardado, o None si no hay uno seteado — a
    diferencia de get_series_name(), no aplica fallback (sirve para saber
    si HAY un nombre custom, ej. para precargar el diálogo de renombrar)."""
    return _load(library, password).get("series", {}).get(series_key, {}).get("display_name")


def get_series_name(library, password, series_key, fallback):
    return get_raw_series_name(library, password, series_key) or fallback


def set_series_name(library, password, series_key, name):
    data = _load(library, password)
    series = data.setdefault("series", {}).setdefault(series_key, {})
    if name:
        series["display_name"] = name
    else:
        series.pop("display_name", None)
    _save(library, password, data)


def get_raw_season_name(library, password, series_key, season_key):
    return (
        _load(library, password)
        .get("series", {})
        .get(series_key, {})
        .get("seasons", {})
        .get(season_key)
    )


def get_season_name(library, password, series_key, season_key, fallback):
    return get_raw_season_name(library, password, series_key, season_key) or fallback


def set_season_name(library, password, series_key, season_key, name):
    data = _load(library, password)
    seasons = data.setdefault("series", {}).setdefault(series_key, {}).setdefault("seasons", {})
    if name:
        seasons[season_key] = name
    else:
        seasons.pop(season_key, None)
    _save(library, password, data)


def get_all_season_names(library, password, series_key):
    """Todos los nombres custom de temporada guardados para esta serie (ej.
    "Gun Buster"/"Die Buster" en Aim for the Top!) — para que el buscador
    de la galería (gallery.py) pueda encontrar la serie por el nombre de
    una temporada, no solo por el nombre de la serie. No incluye
    temporadas sin nombre custom (nada que buscar ahí que ya no esté
    cubierto por el nombre de la serie/carpeta)."""
    return list(
        _load(library, password)
        .get("series", {})
        .get(series_key, {})
        .get("seasons", {})
        .values()
    )


def get_raw_movie_name(library, password, movie_key):
    """Mismo mecanismo que get_raw_series_name, pero en un namespace
    separado ("movies", no "series") — una película no tiene temporadas
    ni episodios, es su propio concepto en el JSON."""
    return _load(library, password).get("movies", {}).get(movie_key, {}).get("display_name")


def get_movie_name(library, password, movie_key, fallback):
    return get_raw_movie_name(library, password, movie_key) or fallback


def set_movie_name(library, password, movie_key, name):
    data = _load(library, password)
    movie = data.setdefault("movies", {}).setdefault(movie_key, {})
    if name:
        movie["display_name"] = name
    else:
        movie.pop("display_name", None)
    _save(library, password, data)


def get_raw_movie_sort_key(library, password, movie_key):
    """Nombre de ordenación invisible (nunca se muestra en la galería,
    solo decide el orden A-Z/Z-A) — pedido explícito del usuario: sagas
    con numeración romana o subtítulos ("Kiddy Grade: Ignition") no
    ordenan bien por título real, y Plex/Jellyfin resuelven esto con un
    campo de "sort title" separado. None si nunca se seteó uno custom,
    en cuyo caso get_movie_sort_key cae al título mostrado (comportamiento
    de siempre, orden alfabético llano)."""
    return _load(library, password).get("movies", {}).get(movie_key, {}).get("sort_key")


def get_movie_sort_key(library, password, movie_key, fallback):
    return get_raw_movie_sort_key(library, password, movie_key) or fallback


def set_movie_sort_key(library, password, movie_key, sort_key):
    data = _load(library, password)
    movie = data.setdefault("movies", {}).setdefault(movie_key, {})
    if sort_key:
        movie["sort_key"] = sort_key
    else:
        movie.pop("sort_key", None)
    _save(library, password, data)


def _episode_key(subpath):
    # Cada parte de subpath es un componente de path individual (nombre
    # de archivo o carpeta) — nunca puede contener "/", así que unirlas
    # con "/" es una key sin ambigüedad y sigue siendo legible si se abre
    # el JSON a mano.
    return "/".join(subpath)


def get_episode_override(library, password, series_key, subpath):
    """{"season": int|None, "kind": "episode"/"extra"/"special"/"opening"/
    "ending", "episode_number": int|None} si el usuario corrigió
    manualmente este episodio (ver PlayerView._apply_episode_correction
    en main.py), o None si nunca se corrigió — en ese caso
    smb_scanner.scan_episodes() usa su clasificación automática de
    siempre. "episode_number" en None significa "no forzar número, solo
    corregir temporada/categoría" — smb_scanner.py lo trata como
    "pinnear" ese episodio en esa posición dentro de su lista (temporada+
    categoría), corriendo al resto (ver _apply_episode_numbers)."""
    return (
        _load(library, password)
        .get("series", {})
        .get(series_key, {})
        .get("episode_overrides", {})
        .get(_episode_key(subpath))
    )


def set_episode_override(library, password, series_key, subpath, season, kind, episode_number=None):
    data = _load(library, password)
    overrides = data.setdefault("series", {}).setdefault(series_key, {}).setdefault("episode_overrides", {})
    overrides[_episode_key(subpath)] = {"season": season, "kind": kind, "episode_number": episode_number}
    _save(library, password, data)


def get_all_episode_overrides(library, password, series_key):
    """dict {subpath (tuple): {"season":.., "kind":..}} — para pasarle
    directo a smb_scanner.scan_episodes(overrides=...). Reconstruye las
    keys de vuelta a tuple (se guardan como string unidas con "/")."""
    raw = (
        _load(library, password)
        .get("series", {})
        .get(series_key, {})
        .get("episode_overrides", {})
    )
    return {tuple(key.split("/")): value for key, value in raw.items()}


def get_track_probe(library, password, series_key):
    """Idiomas de audio/subtítulo reales encontrados sondeando un episodio
    de la serie (ver track_probe.py) — {"audio": [...], "subtitle": [...]},
    o None si todavía no se sondeó nunca. Se guarda server-side (no en
    track_prefs.py, que es local a esta máquina) para no tener que volver
    a abrir un episodio con libVLC solo para esto en cada instalación."""
    return _load(library, password).get("series", {}).get(series_key, {}).get("track_probe")


def set_track_probe(library, password, series_key, probe):
    data = _load(library, password)
    data.setdefault("series", {}).setdefault(series_key, {})["track_probe"] = probe
    _save(library, password, data)
