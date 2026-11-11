"""
Scanner de bibliotecas — SMB (vía smbclient/smbprotocol) o carpetas
locales del disco, según library.connection_type (ver library_store.py).
No monta nada en el filesystem para SMB: lista carpetas y lee archivos
chicos (posters) directo por SMB. La reproducción de video NO pasa por
acá — libVLC abre su propio smb:///path local (ver smb_paths.vlc_url y
playback_target() más abajo).

register()/scandir()/open_binary()/open_text() son la abstracción que
esconde la diferencia SMB/local — el resto de las funciones de este
módulo (y library_metadata.py) las usan sin preguntarse cuál es.
"""

import os
import re
from dataclasses import dataclass

import smbclient

from smb_paths import unc_path, vlc_credential_options, vlc_url

EPISODE_GROUP_ROOT = "Episodios"

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".m4v", ".ts", ".webm", ".mov"}
CONNECTION_TIMEOUT = 8  # segundos; el default de smbprotocol (60s) es muy largo para la UI
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")
# Los packs de fansub no siguen ninguna convención real (serie.jpg,
# Tem1.jpg, serie-cover2.jpg, nombre-corto.jpg...). En vez de una lista fija
# de nombres, se busca cualquier imagen con una de estas palabras en el
# nombre, y si no hay match pero la carpeta tiene una sola imagen suelta se
# asume que es el poster. Si hay varias imágenes ambiguas (ej. capturas de
# pantalla Tem1.jpg..Tem5.jpg) sin ninguna palabra clave, se deja sin poster
# antes que adivinar mal.
POSTER_KEYWORDS = ("cover", "poster", "folder")


class StorageError(Exception):
    pass


@dataclass
class SeriesEntry:
    name: str
    subpath: tuple  # partes de path relativas a la raíz de la biblioteca
    poster_subpath: tuple | None


@dataclass
class MovieEntry:
    name: str
    subpath: tuple
    poster_subpath: tuple | None


@dataclass
class EpisodeEntry:
    name: str  # nombre para mostrar (sin extensión)
    subpath: tuple  # path completo relativo a la biblioteca, listo para reproducir


@dataclass
class EpisodeGroup:
    key: str  # identificador estable para guardar nombres custom (nombre de carpeta, o "S01" sintético)
    label: str  # nombre por defecto a mostrar si no hay uno custom guardado
    season_number: object  # int, o None si no se pudo determinar (ej. "Specials") — para buscar carátula TemN
    episodes: list  # numerados normal
    extras: list  # "Special"/OVA de ESTA temporada — separados de la numeración normal, pero agrupados con la temporada
    openings: list
    endings: list


def register(library, password):
    """Sesión SMB — no-op para bibliotecas locales (no hay nada que
    autenticar). Público: lo usan otros módulos (ej. library_metadata.py)
    que también necesitan la sesión activa antes de leer/escribir, sin
    duplicar esta lógica."""
    if library.connection_type == "local":
        return
    try:
        smbclient.register_session(
            library.server,
            username=library.username,
            password=password,
            connection_timeout=CONNECTION_TIMEOUT,
        )
    except Exception as exc:
        raise StorageError(
            f"No se pudo conectar a {library.server}: {exc}"
        ) from exc


def _local_path(library, *subparts):
    return os.path.join(library.path, *subparts)


def scandir(library, *subparts):
    """Lista una carpeta de la biblioteca (SMB o local, según
    connection_type) — devuelve entries con la misma interfaz en los dos
    casos (.name/.is_file()/.is_dir()): os.DirEntry (local) y las
    entries de smbclient (SMB) ya la comparten, así que el resto del
    código no necesita ninguna rama para consumirlas."""
    if library.connection_type == "local":
        return list(os.scandir(_local_path(library, *subparts)))
    return list(smbclient.scandir(unc_path(library.server, library.share, library.path, *subparts)))


def open_binary(library, *subparts):
    if library.connection_type == "local":
        return open(_local_path(library, *subparts), "rb")
    return smbclient.open_file(unc_path(library.server, library.share, library.path, *subparts), mode="rb")


def open_text(library, *subparts, mode="r"):
    if library.connection_type == "local":
        return open(_local_path(library, *subparts), mode=mode, encoding="utf-8")
    return smbclient.open_file(
        unc_path(library.server, library.share, library.path, *subparts), mode=mode, encoding="utf-8",
    )




def playback_target(library, password, *subparts):
    """(url_o_path, opciones_extra_de_vlc) — listo para pasarle directo a
    vlc.Instance.media_new()/media.add_option(). Local: libVLC abre un
    path absoluto de disco directo, sin URL ni credenciales."""
    if library.connection_type == "local":
        return _local_path(library, *subparts), []
    url = vlc_url(library.server, library.share, library.path, *subparts)
    options = vlc_credential_options(library.username, password)
    return url, options


def _is_video(filename):
    return any(filename.lower().endswith(ext) for ext in VIDEO_EXTENSIONS)


def _is_image(filename):
    return any(filename.lower().endswith(ext) for ext in IMAGE_EXTENSIONS)


def find_poster(library, *subparts):
    """Busca un poster dentro de una carpeta (no recursivo). Devuelve el
    subpath relativo del poster encontrado, o None."""
    try:
        entries = scandir(library, *subparts)
    except Exception:
        return None
    images = [e.name for e in entries if e.is_file() and _is_image(e.name)]
    for name in images:
        if any(keyword in name.lower() for keyword in POSTER_KEYWORDS):
            return (*subparts, name)
    if len(images) == 1:
        return (*subparts, images[0])
    return None


def test_connection(library, password):
    """Smoke test usado por el diálogo de 'agregar biblioteca'. Levanta
    StorageError con un mensaje legible si algo falla."""
    register(library, password)
    try:
        scandir(library)
    except Exception as exc:
        if library.connection_type == "local":
            raise StorageError(f"No se pudo leer la carpeta '{library.path}': {exc}") from exc
        raise StorageError(
            f"Conectado a {library.server}, pero no se pudo listar "
            f"'{library.share}/{library.path}': {exc}"
        ) from exc


def scan_series(library, password):
    register(library, password)
    result = []
    for entry in scandir(library):
        if not entry.is_dir():
            continue
        poster = find_poster(library, entry.name)
        result.append(SeriesEntry(name=entry.name, subpath=(entry.name,), poster_subpath=poster))
    result.sort(key=lambda s: s.name.lower())
    return result


def scan_movies(library, password):
    register(library, password)
    entries = scandir(library)
    files_by_stem = {}
    for entry in entries:
        if entry.is_file():
            files_by_stem.setdefault(stem(entry.name), []).append(entry.name)

    result = []
    for entry in entries:
        if not entry.is_file() or not _is_video(entry.name):
            continue
        movie_stem = stem(entry.name)
        poster = None
        for sibling in files_by_stem.get(movie_stem, []):
            if _is_image(sibling):
                poster = (sibling,)
                break
        result.append(MovieEntry(name=movie_stem, subpath=(entry.name,), poster_subpath=poster))
    result.sort(key=lambda m: m.name.lower())
    return result


def stem(filename):
    """Público (no _stem) — main.py PlayerView lo necesita para resolver
    la MISMA key de metadata que usa el scan de acá (MovieEntry.name,
    ver scan_movies): el subpath de una película es (nombre_de_archivo,)
    CON extensión, pero la key bajo la que library_metadata guarda
    nombre/orden custom de una película es el stem SIN extensión — un
    caller que arme esa key con el subpath completo en vez de este
    helper (bug real encontrado en vivo probando el campo de orden antes
    de compilar el Flatpak, Paso 36: _rename_movie ya lo hacía mal desde
    antes) guarda bajo una key que el próximo escaneo nunca vuelve a
    leer — el valor queda huérfano en el JSON, invisible salvo por el
    parche en memoria del propio movie_renamed dentro de la misma
    sesión."""
    idx = filename.rfind(".")
    return filename if idx == -1 else filename[:idx]


def _natural_sort_key(name):
    """'S01E02' antes que 'S01E10': separa dígitos de texto y compara los
    dígitos como número, no como string."""
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", name)]


# Para agrupar por temporada aunque no haya subcarpetas (ej. una serie
# con 79 episodios sueltos en la raíz, sin ninguna subcarpeta real — la
# temporada solo se puede saber leyendo el nombre del archivo).
_SEASON_PATTERNS = (
    re.compile(r"[Ss](\d{1,2})[Ee]\d{1,3}"),  # S01E01
    re.compile(r"(?<!\d)(\d{1,2})x\d{1,3}(?!\d)"),  # 1x01
)
_SEASON_ZERO_PATTERN = re.compile(r"[Ss]00[Ee]\d{1,3}")

# Clasificación de "qué es" un video, además de a qué temporada
# pertenece. Cada patrón sale de nombres de archivo reales: NCOP/NCED
# sueltos, OP.mkv/ED.mkv en una subcarpeta, "SxxEyy Special" en el tag de
# release.
_OPENING_PATTERNS = (
    re.compile(r"\bncop\b", re.IGNORECASE),
    re.compile(r"\bopening\b", re.IGNORECASE),
    re.compile(r"\bop\d*\b", re.IGNORECASE),
)
_ENDING_PATTERNS = (
    re.compile(r"\bnced\b", re.IGNORECASE),
    re.compile(r"\bending\b", re.IGNORECASE),
    re.compile(r"\bed\d*\b", re.IGNORECASE),
)
# "special" a secas es demasiado ambiguo: puede ser un tag de release
# ("S04E01 Special [BD ...]") o parte del título narrativo de un
# episodio normal ("S01E11 - Limited Special Offer Day" — un falso
# positivo real que apareció probando esto). Para distinguir, "special" solo cuenta si
# aparece PEGADO al código de episodio (con solo un espacio de por
# medio) — un tag de release va justo al lado, un título narrativo tiene
# palabras propias en el medio.
_SPECIAL_TAG_PATTERN = re.compile(r"[Ss]\d{1,2}[Ee]\d{1,3}\s*special\b", re.IGNORECASE)
_EXTRA_NAME_PATTERN = re.compile(r"\b(especial|especiales|ova)\b", re.IGNORECASE)

# Carpetas que NO son una temporada — su sola presencia ya dice qué es su
# contenido, aunque el nombre de archivo adentro no lo diga por sí solo.
_OPENING_ENDING_FOLDER_NAMES = {"nc", "ncop", "nced", "op", "ed", "openings", "endings"}
_BONUS_FOLDER_NAMES = {"ova", "ovas", "extra", "extras"}
_SPECIAL_FOLDER_NAMES = {"special", "specials", "especial", "especiales"}
_SIDE_CONTENT_FOLDER_NAMES = _OPENING_ENDING_FOLDER_NAMES | _BONUS_FOLDER_NAMES

SPECIALS_GROUP_KEY = "ESPECIALES"


def _extract_season_number(filename):
    for pattern in _SEASON_PATTERNS:
        match = pattern.search(filename)
        if match:
            return int(match.group(1))
    return None


def _extract_season_number_from_folder(folder_name):
    match = re.search(r"(\d{1,2})", folder_name)
    return int(match.group(1)) if match else None


def _classify_kind(filename, folder_hint=None):
    """episode (default) / opening / ending / extra / special.
    folder_hint es el fallback cuando el archivo está adentro de una
    carpeta tipo NC/OVA dedicada pero su propio nombre no se autoidentifica
    (poco común en la práctica, pero mejor que asumir 'episodio normal')."""
    if _SEASON_ZERO_PATTERN.search(filename):
        return "special"
    if any(p.search(filename) for p in _OPENING_PATTERNS):
        return "opening"
    if any(p.search(filename) for p in _ENDING_PATTERNS):
        return "ending"
    if _SPECIAL_TAG_PATTERN.search(filename) or _EXTRA_NAME_PATTERN.search(filename):
        return "extra"
    return folder_hint or "episode"


def scan_episodes(library, password, series_subpath, overrides=None):
    """Escanea una serie. Soporta episodios sueltos directo en la carpeta
    de la serie y/o subcarpetas de temporada (Season 1, S1...), con
    agrupamiento por temporada leyendo SxxExx/NxNN del nombre de archivo
    cuando no hay subcarpetas reales (ver Paso 11). Además, separa de la
    numeración normal (Paso 12):

    - **Especiales de toda la serie**: episodios `S00Exx`, o cualquier
      carpeta llamada "Specials"/"Special" (en la raíz o dentro de una
      temporada) — van a un único grupo "Especiales" al FINAL de todo,
      nunca se tratan como "temporada 0".
    - **Extras de una temporada específica** (`SxxEyy ... Special`, o
      contenido de una carpeta "OVA(s)"/"Extra(s)"): quedan aparte de los
      episodios numerados de esa temporada, pero siguen agrupados con
      ella (no se van al bloque de especiales de toda la serie).
    - **Openings/Endings de una temporada** (`NCOP`/`NCED`/`OP`/`ED`, o
      contenido de una carpeta "NC"/"OP"/"ED"): también aparte de los
      episodios numerados de esa temporada.

    Baja un nivel de subcarpetas desde la raíz de la serie, y UN nivel
    más específicamente para carpetas de temporada (para agarrar
    NC/OVAs/Specials anidados adentro) — no es
    recursivo más profundo que eso a propósito.

    'overrides': dict {subpath (tuple): {"season": int|None, "kind": str}}
    — correcciones manuales guardadas por el usuario (ver
    PlayerView._apply_episode_correction en main.py, guardadas vía
    library_metadata.set_episode_override). Si un archivo tiene un
    override, se usa TAL CUAL en vez de la clasificación automática por
    nombre/carpeta — pensado para los casos raros donde el heurístico se
    equivoca (ver Paso 13, falso positivo real con "Special" en el
    título)."""
    register(library, password)
    entries = scandir(library, *series_subpath)
    overrides = overrides or {}

    seasons = {}  # season_number (o None) -> {"episodes": [], "extras": [], "openings": [], "endings": []}
    specials = []  # bloque final de TODA la serie

    def season_bucket(season_num):
        return seasons.setdefault(
            season_num, {"episodes": [], "extras": [], "openings": [], "endings": []}
        )

    def route_file(filename, subpath_tuple, known_season=None, folder_hint=None, known_kind=None):
        override = overrides.get(subpath_tuple)
        if override:
            kind = override["kind"]
            season_num = override["season"]
        elif known_kind:
            kind = known_kind
            season_num = known_season if known_season is not None else _extract_season_number(filename)
        else:
            kind = _classify_kind(filename, folder_hint=folder_hint)
            season_num = known_season if known_season is not None else _extract_season_number(filename)
        entry = EpisodeEntry(name=stem(filename), subpath=subpath_tuple)

        if kind == "special" or season_num == 0:
            specials.append(entry)
            return
        bucket = season_bucket(season_num)
        bucket_name = {"episode": "episodes", "extra": "extras", "opening": "openings", "ending": "endings"}[kind]
        bucket[bucket_name].append(entry)

    def collect(*subparts):
        try:
            return scandir(library, *subparts)
        except Exception:
            return []

    # 1) archivos sueltos directo en la raíz de la serie
    for entry in entries:
        if entry.is_file() and _is_video(entry.name):
            route_file(entry.name, (*series_subpath, entry.name))

    # 2) subcarpetas de la raíz
    for entry in entries:
        if not entry.is_dir():
            continue
        folder_name = entry.name
        lower = folder_name.lower()

        if lower in _SPECIAL_FOLDER_NAMES:
            for fe in collect(*series_subpath, folder_name):
                if fe.is_file() and _is_video(fe.name):
                    route_file(fe.name, (*series_subpath, folder_name, fe.name), known_kind="special")
            continue

        if lower in _SIDE_CONTENT_FOLDER_NAMES:
            hint = "opening" if lower in _OPENING_ENDING_FOLDER_NAMES else "extra"
            for fe in collect(*series_subpath, folder_name):
                if fe.is_file() and _is_video(fe.name):
                    route_file(fe.name, (*series_subpath, folder_name, fe.name), folder_hint=hint)
            continue

        # Carpeta de temporada "normal" — sus propios videos, más un nivel
        # más buscando NC/OVAs/Specials anidados (ej. S1/NC, S2/OVA).
        season_num_from_folder = _extract_season_number_from_folder(folder_name)
        for fe in collect(*series_subpath, folder_name):
            if fe.is_file() and _is_video(fe.name):
                route_file(fe.name, (*series_subpath, folder_name, fe.name), known_season=season_num_from_folder)
            elif fe.is_dir():
                nested_lower = fe.name.lower()
                if nested_lower in _SPECIAL_FOLDER_NAMES:
                    for ne in collect(*series_subpath, folder_name, fe.name):
                        if ne.is_file() and _is_video(ne.name):
                            route_file(
                                ne.name, (*series_subpath, folder_name, fe.name, ne.name),
                                known_kind="special",
                            )
                elif nested_lower in _SIDE_CONTENT_FOLDER_NAMES:
                    hint = "opening" if nested_lower in _OPENING_ENDING_FOLDER_NAMES else "extra"
                    for ne in collect(*series_subpath, folder_name, fe.name):
                        if ne.is_file() and _is_video(ne.name):
                            route_file(
                                ne.name, (*series_subpath, folder_name, fe.name, ne.name),
                                known_season=season_num_from_folder, folder_hint=hint,
                            )
                # otras subcarpetas anidadas (ej. "Subs") se ignoran a propósito

    def sorted_entries(items):
        naturally_sorted = sorted(items, key=lambda ep: _natural_sort_key(ep.name))
        pinned = {}
        unpinned = []
        for entry in naturally_sorted:
            number = (overrides.get(entry.subpath) or {}).get("episode_number")
            if number:
                pinned[number] = entry
            else:
                unpinned.append(entry)
        if not pinned:
            return naturally_sorted

        # "Pinnear" un episodio en la posición N corre al resto — se
        # recorren las posiciones en orden, llenando con el pinneado si
        # hay uno ahí, si no con el próximo no-pinneado en su orden
        # natural (mismo criterio intuitivo que "insertar en la posición
        # N" de cualquier lista). Si sobran pinneados con número mayor al
        # total de items (ej. pinnear el episodio 50 de una lista de 12),
        # van al final ordenados por número.
        result = []
        idx = 0
        position = 1
        while idx < len(unpinned) or pinned:
            if position in pinned:
                result.append(pinned.pop(position))
            elif idx < len(unpinned):
                result.append(unpinned[idx])
                idx += 1
            else:
                break
            position += 1
        for number in sorted(pinned):
            result.append(pinned[number])
        return result

    groups = []
    for season_num in sorted(k for k in seasons if k is not None):
        bucket = seasons[season_num]
        if not any(bucket.values()):
            continue
        groups.append(EpisodeGroup(
            key=f"S{season_num:02d}",
            label=f"Temporada {season_num}",
            season_number=season_num,
            episodes=sorted_entries(bucket["episodes"]),
            extras=sorted_entries(bucket["extras"]),
            openings=sorted_entries(bucket["openings"]),
            endings=sorted_entries(bucket["endings"]),
        ))

    if None in seasons and any(seasons[None].values()):
        bucket = seasons[None]
        groups.append(EpisodeGroup(
            key=EPISODE_GROUP_ROOT,
            label=EPISODE_GROUP_ROOT,
            season_number=None,
            episodes=sorted_entries(bucket["episodes"]),
            extras=sorted_entries(bucket["extras"]),
            openings=sorted_entries(bucket["openings"]),
            endings=sorted_entries(bucket["endings"]),
        ))

    if specials:
        groups.append(EpisodeGroup(
            key=SPECIALS_GROUP_KEY,
            label="Especiales",
            season_number=None,
            episodes=sorted_entries(specials),
            extras=[],
            openings=[],
            endings=[],
        ))

    return groups


def find_season_poster(library, series_subpath, season_number):
    """Busca una carátula de temporada tipo 'TemN.jpg' (convención de los
    packs de fansub de esta biblioteca) directo en la carpeta de la serie
    — no en la subcarpeta de la temporada."""
    if season_number is None:
        return None
    candidates = {f"tem{season_number}{ext}" for ext in IMAGE_EXTENSIONS}
    try:
        entries = scandir(library, *series_subpath)
    except Exception:
        return None
    for entry in entries:
        if entry.is_file() and entry.name.lower() in candidates:
            return (*series_subpath, entry.name)
    return None


def fetch_poster_bytes(library, poster_subpath):
    if not poster_subpath:
        return None
    try:
        with open_binary(library, *poster_subpath) as f:
            return f.read()
    except Exception:
        return None
