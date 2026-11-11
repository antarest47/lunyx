"""
Mecánica compartida por progress_store.py y track_prefs.py para que cada
biblioteca pueda, opcionalmente, centralizar su progreso/preferencias de
pista en un share SMB o carpeta local distinta (en vez del store local de
siempre en ~/.config/lunyx/) — ver Library.progress_* (library_store.py).

Diseño "un archivo por dispositivo" (CONTEXT.md, Paso 38+): cada
instalación escribe SOLO su propio archivo remoto
(progress.<device_id>.json / track_prefs.<device_id>.json, ver
app_settings.get_device_id) y nunca toca el de otro dispositivo — así dos
equipos guardando progreso de series DISTINTAS al mismo tiempo nunca se
pisan (no hay ningún documento compartido que uno pueda sobreescribir
encima del otro). Al leer, se fusionan TODOS los archivos que haya (ver
merge_device_payloads, arriba en este mismo módulo).

Todo lo remoto pasa por acá para nunca bloquear el hilo de Qt:
- Las escrituras SIEMPRE van al archivo LOCAL primero, síncronas (I/O de
  disco chico, instantáneo) — nunca se pierde nada por un crash antes de
  llegar a subirlo. set_entry/delete_entry/delete_prefix.
- Subir al remoto es un paso EXPLÍCITO y separado (push()), disparado en
  checkpoints (pausa = sin bloquear; "Volver"/cierre = bloqueante con
  tope) — no en cada guardado. Ver progress_store.checkpoint().
- Lecturas SIEMPRE de memoria (el envelope local, que es barato de leer
  de disco, + la cache de "otros dispositivos" poblada por preload()) —
  nunca I/O directo en el momento de leer.
- notifier.offline_changed avisa a la UI cuando un target no se pudo
  alcanzar, para mostrar un aviso (ver MainWindow en main.py).
"""

import errno
import json
import re
import threading
import time
from dataclasses import dataclass

from PySide6.QtCore import QObject, QThread, Signal

import app_settings
import credentials
import library_metadata
import smb_scanner

# Ver RemoteJsonStore._read_device_envelope — reintentos ante JSON
# ilegible (posible lectura a medio camino de una escritura real, ver
# CONTEXT.md Paso 34).
_READ_RETRY_ATTEMPTS = 3
_READ_RETRY_DELAY_S = 0.15

# ---- envelope y merge (Paso 36+, "un archivo por dispositivo") ----
#
# Reemplaza el diseño viejo (un solo progress.json/track_prefs.json
# COMPARTIDO, donde cada guardado remoto subía el documento ENTERO —
# "mi vista en memoria es la verdad para todo el archivo") por uno donde
# cada instalación escribe SOLO su propio archivo remoto
# (progress.<device_id>.json, ver app_settings.get_device_id) y nunca
# toca el de otro dispositivo. El conflicto de raíz (dos equipos
# guardando progreso de series DISTINTAS al mismo tiempo se pisaban)
# desaparece por construcción: nunca hay dos escritores posibles del
# mismo archivo. Al LEER, se listan y fusionan TODOS los archivos que
# haya (ver RemoteJsonStore.preload en la reescritura de más abajo) —
# entrada por entrada, ganando el updated_at más reciente.
#
# "Envelope": la forma que tiene tanto el archivo LOCAL
# (~/.config/lunyx/progress.json/track_prefs.json) como cada archivo
# REMOTO — son literalmente el mismo documento, uno es la copia que
# viaja al share. Guarda tres cosas:
#   - entries: {clave: {..valor.., "updated_at": epoch}} — vivas.
#   - tombstones: {clave: updated_at} — claves que ESTE dispositivo tenía
#     y borró (ej. episodio terminado, ver progress_store.FINISHED_THRESHOLD).
#     Hace falta para que el merge no la resucite si otro dispositivo
#     todavía tiene una copia vieja sin borrar de esa misma clave.
#   - prefix_tombstones: [{"prefix": str, "updated_at": epoch}] — borrado
#     en bloque ("marcar serie como vista", progress_store.delete_series_entries)
#     — ver merge_device_payloads() para cómo se aplica contra entradas
#     de CUALQUIER dispositivo, no solo las que este dispositivo conoce.
_ENVELOPE_SCHEMA = 2


def _empty_envelope(device_id):
    return {
        "schema": _ENVELOPE_SCHEMA,
        "device_id": device_id,
        "entries": {},
        "tombstones": {},
        "prefix_tombstones": [],
    }


def _normalize_envelope(data, device_id):
    """Devuelve un envelope schema 2 válido a partir de lo que sea que
    haya en disco (o `data=None` si el archivo no existía todavía).

    Formato viejo (Paso 35 y anteriores): un dict PLANO `{clave: entrada}`,
    sin ninguna key "schema" — se envuelve tal cual como `entries`, sin
    tombstones/prefix_tombstones (nunca los tuvo). Mismo criterio que ya
    usa el proyecto para archivos de config viejos (copiar/envolver,
    nunca romper) — el usuario no tiene que hacer nada a mano. Distinto
    del archivo COMPARTIDO viejo (progress.json sin sufijo de
    dispositivo, de antes de este rediseño): ese se ignora del todo por
    decisión explícita del usuario (no matchea la regex de listado de
    RemoteJsonStore.preload), esto de acá es sobre el formato interno de
    UN archivo (local o de un dispositivo), no sobre cuáles se leen.

    Un envelope schema 2 ya armado pasa con las tres listas rellenadas a
    default si faltan (defensivo ante un archivo escrito por una versión
    futura con menos campos, o corrupción parcial) — nunca lanza."""
    if not data:
        return _empty_envelope(device_id)
    if "schema" not in data:
        return {
            "schema": _ENVELOPE_SCHEMA,
            "device_id": device_id,
            "entries": dict(data),
            "tombstones": {},
            "prefix_tombstones": [],
        }
    return {
        "schema": _ENVELOPE_SCHEMA,
        "device_id": data.get("device_id", device_id),
        "entries": dict(data.get("entries") or {}),
        "tombstones": dict(data.get("tombstones") or {}),
        "prefix_tombstones": list(data.get("prefix_tombstones") or []),
    }


def _consider(signals, key, updated_at, kind, entry):
    """Actualiza `signals[key]` si esta señal (viva o muerta, con su
    updated_at) es más nueva que la que ya había considerada — o, ante un
    EMPATE exacto de updated_at, si esta es la muerta (conservador: ante
    la duda entre "vivo" y "borrado" al mismo instante, gana el borrado,
    nunca al revés — así una resurrección accidental por timestamps
    idénticos es imposible)."""
    current = signals.get(key)
    if current is None or updated_at > current[0] or (updated_at == current[0] and kind == "dead"):
        signals[key] = (updated_at, kind, entry)


def merge_device_payloads(payloads):
    """Fusiona los envelopes de TODOS los dispositivos (incluido el
    propio — ver RemoteJsonStore.get(), que le pasa la copia LOCAL fresca
    de este dispositivo en vez de su última copia remota conocida, que
    puede estar un paso atrás de un push todavía en vuelo) en un solo
    dict `{clave: entrada}` — la forma que progress_store/track_prefs
    esperan. Función PURA: sin Qt, sin I/O, sin threads — pensada para
    testearse aislada (ver tests/test_progress_merge.py), es la pieza
    con más responsabilidad de todo el rediseño (acá es donde se decide
    qué dato sobrevive y cuál se descarta).

    Todo el orden depende de `updated_at` de reloj LOCAL de cada
    dispositivo — no hay vector clocks ni ningún reloj central. Un reloj
    desincronizado entre equipos es un riesgo aceptado del diseño (podría
    hacer ganar una entrada más vieja en los hechos, o tombstonear
    progreso "futuro" real de otro equipo si el reloj adelantado
    pertenece al que borra) — no hay mitigación acá, quedó fuera de
    alcance a propósito (mismo tipo de asunción que ya hacía el sistema
    viejo con FINISHED_THRESHOLD y compañía)."""
    signals = {}  # clave -> (updated_at, "live"|"dead", entry_o_None)
    for payload in payloads:
        for key, entry in payload.get("entries", {}).items():
            _consider(signals, key, entry.get("updated_at", 0), "live", entry)
        for key, updated_at in payload.get("tombstones", {}).items():
            _consider(signals, key, updated_at, "dead", None)

    prefix_tombstones = [pt for payload in payloads for pt in payload.get("prefix_tombstones", [])]
    if prefix_tombstones:
        # "Marcar serie como vista" tombstonea por PREFIJO, y tiene que
        # tapar entradas de CUALQUIER dispositivo (incluido uno que nunca
        # se enteró de que esa clave existía) — ver progress_store.
        # delete_series_entries. Por eso esto es una pasada aparte,
        # DESPUÉS de resolver entries/tombstones puntuales: una entrada
        # viva más nueva que el tombstone de prefijo sigue viva (alguien
        # la retomó después de que se marcó la serie como vista), una más
        # vieja se tapa sin importar si ESTE dispositivo tiene o no un
        # tombstone puntual propio para ella.
        for key, (updated_at, kind, _entry) in list(signals.items()):
            if kind == "dead":
                continue
            for pt in prefix_tombstones:
                if updated_at <= pt["updated_at"] and key.startswith(pt["prefix"]):
                    signals[key] = (updated_at, "dead", None)
                    break

    return {key: entry for key, (_updated_at, kind, entry) in signals.items() if kind == "live"}


@dataclass(frozen=True)
class StorageTarget:
    """Duck-type compatible con smb_scanner (.connection_type/.server/
    .share/.path/.username) — NO es una Library real, es la ubicación
    centralizada elegida para progress.json/track_prefs.json de una
    biblioteca (puede vivir en un server/share sin relación con la
    biblioteca misma)."""

    connection_type: str
    server: str = ""
    share: str = ""
    path: str = ""
    username: str = ""


def target_for_library(library):
    """None si la biblioteca usa el store local de siempre (default)."""
    if not getattr(library, "progress_centralized", False):
        return None
    return StorageTarget(
        connection_type=library.progress_connection_type,
        server=library.progress_server,
        share=library.progress_share,
        path=library.progress_path,
        username=library.progress_username,
    )


def target_key(target):
    """Identifica el ARCHIVO real, no la biblioteca — si dos bibliotecas
    apuntan al mismo share/carpeta como progreso centralizado (uso
    esperado: comparten un solo progress.json), comparten una sola
    entrada de cache y un solo flush en vez de pisarse entre sí. Las
    entradas ADENTRO del JSON ya van prefijadas por library_id (ver
    progress_store._key), así que compartir el archivo es seguro."""
    if target is None:
        return None
    if target.connection_type == "local":
        return ("local", target.path)
    return ("smb", target.server, target.share, target.path)


# library.id (local) -> uid compartido leído del sidecar de la biblioteca.
# Solo se puebla para bibliotecas centralizadas (ver resolve_library_uid).
_library_uid_cache = {}


def resolve_library_uid(library, library_password):
    """Resuelve (y cachea) la identidad compartida de la biblioteca —
    LLAMAR DESDE UN QThread, hace I/O de red contra el sidecar de la
    biblioteca (ver library_metadata.get_or_create_library_uid, que
    explica por qué el Library.id local no sirve para centralizar).

    Ojo: `library_password` es la contraseña de la BIBLIOTECA (donde vive
    el sidecar), no la del target de progreso — pueden ser servidores y
    usuarios totalmente distintos. Devuelve None si no se pudo resolver
    (share caído): en ese caso key_prefix() también da None y los stores
    se abstienen de leer/escribir, en vez de arriesgarse a usar una clave
    equivocada."""
    if not getattr(library, "progress_centralized", False):
        return library.id
    if library.id in _library_uid_cache:
        return _library_uid_cache[library.id]
    try:
        uid = library_metadata.get_or_create_library_uid(library, library_password)
    except Exception:
        return None
    _library_uid_cache[library.id] = uid
    return uid


def key_prefix(library):
    """Con qué identificador se guardan las entradas de esta biblioteca:
    el uid COMPARTIDO si centraliza (así la laptop/el escritorio/el HTPC
    escriben todos en el mismo compartimento), o el id local de siempre si
    no centraliza (sin cambios, no invalida progreso local ya guardado).
    None = centraliza pero el uid todavía no se resolvió (ver
    resolve_library_uid)."""
    if not getattr(library, "progress_centralized", False):
        return library.id
    return _library_uid_cache.get(library.id)


def forget_library_uid(library_id):
    _library_uid_cache.pop(library_id, None)


def _progress_service_id(library):
    return f"{library.id}:progress"


def progress_password(library):
    """Contraseña de la conexión de PROGRESO (puede ser un server/usuario
    totalmente distinto al de la biblioteca misma) — service-key propio en
    keyring, nunca se mezcla con credentials de la biblioteca."""
    return credentials.get_password(_progress_service_id(library), library.progress_username) or ""


def set_progress_password(library, password):
    if password:
        credentials.set_password(_progress_service_id(library), library.progress_username, password)


def delete_progress_password(library):
    credentials.delete_password(_progress_service_id(library), library.progress_username)


def target_label(library):
    target = target_for_library(library)
    if target is None:
        return ""
    if target.connection_type == "local":
        return target.path
    return f"{target.server}/{target.share}"


class _Notifier(QObject):
    """Singleton — un solo punto de conexión para MainWindow, sin importar
    cuál de los dos stores (progreso/prefs) sea el que falló."""

    offline_changed = Signal(bool, str)  # (offline, label legible del target)


notifier = _Notifier()


class RemoteJsonStore:
    """Una instancia por archivo lógico (progreso / preferencias de
    pista). local_path: Path completo del archivo LOCAL (ya resuelto por
    el caller, con su propia lógica de XDG_CONFIG_HOME/migración de
    nombre viejo de proyecto — este módulo no la duplica). remote_prefix:
    prefijo del nombre de archivo remoto ("progress"/"track_prefs") — el
    nombre real de cada archivo lleva el device_id de quien lo escribió
    (ver _remote_filename), nunca un nombre fijo compartido.

    El archivo LOCAL y cada archivo REMOTO tienen la misma forma —
    "envelope", ver _empty_envelope/_normalize_envelope/
    merge_device_payloads más arriba en este módulo."""

    def __init__(self, local_path, remote_prefix):
        self._local_path = local_path
        self._remote_prefix = remote_prefix
        self._device_file_re = re.compile(
            rf"^{re.escape(remote_prefix)}\.([0-9a-f]{{6,32}})\.json$"
        )
        # target_key -> {"other_devices": {device_id: envelope}, "state": "ready"|"unavailable"}
        self._remote_cache = {}
        self._flushing = set()  # target_keys con un push en vuelo ahora
        self._pending_rerun = {}  # target_key -> library, "correr otro push al terminar este"
        self._flush_workers = []  # referencias vivas, evita garbage collection de QThreads en vuelo
        self._lock = threading.Lock()

    # ---- local: SIEMPRE la fuente de verdad para lo propio, nunca bloquea ----

    def _own_device_id(self):
        return app_settings.get_device_id()

    def _load_local_envelope(self):
        if not self._local_path.exists():
            return _empty_envelope(self._own_device_id())
        with open(self._local_path, "r", encoding="utf-8") as f:
            try:
                data = json.load(f)
            except json.JSONDecodeError:
                # Corrupción local real (ej. corte de luz a mitad de un
                # write anterior) — nadie MÁS escribe este archivo al
                # mismo tiempo que este proceso (a diferencia del remoto,
                # que sí puede tener otro dispositivo escribiendo el
                # suyo), así que no hace falta el retry de más abajo acá.
                # Degradar a "no había nada" es preferible a que la app
                # no arranque.
                return _empty_envelope(self._own_device_id())
        return _normalize_envelope(data, self._own_device_id())

    def _write_local_envelope(self, envelope):
        self._local_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._local_path, "w", encoding="utf-8") as f:
            json.dump(envelope, f, indent=2, ensure_ascii=False)

    def _mutate_local(self, mutate_fn):
        """mutate_fn(envelope) -> envelope'. Síncrono siempre (I/O de
        disco chico, instantáneo) — nunca dispara ningún push por sí
        solo, eso es responsabilidad explícita de push() (ver
        progress_store.checkpoint)."""
        envelope = mutate_fn(self._load_local_envelope())
        self._write_local_envelope(envelope)
        return envelope

    def _get_local_only(self):
        return merge_device_payloads([self._load_local_envelope()])

    # ---- remoto: lectura (listar + leer los archivos de OTROS dispositivos) ----

    def _read_device_envelope(self, target, filename):
        """Lee UN archivo remoto de UN dispositivo — mismo retry ante
        lectura a medio escribir que Paso 34 (ver CONTEXT.md), ahora
        sobre una ventana de colisión mucho más rara: cada archivo tiene
        UN escritor posible (su propio dueño), que además solo escribe en
        checkpoints puntuales, no cada 5s como el diseño viejo.

        Un archivo puntual ilegible tras los reintentos se trata como
        vacío — un dispositivo con problemas no debe tirar abajo la
        lectura de TODOS los demás. `FileNotFoundError`/ENOENT (se borró
        entre el listado y la lectura) también vacío. Errores de conexión
        reales SÍ se propagan (StorageError) — esos abortan el preload
        completo, ver _read_other_device_envelopes."""
        for attempt in range(_READ_RETRY_ATTEMPTS):
            try:
                with smb_scanner.open_text(target, filename, mode="r") as f:
                    return _normalize_envelope(json.load(f), device_id=None)
            except FileNotFoundError:
                return _empty_envelope(None)
            except OSError as exc:
                if getattr(exc, "errno", None) == errno.ENOENT:
                    return _empty_envelope(None)
                raise smb_scanner.StorageError(f"No se pudo leer {filename}: {exc}") from exc
            except json.JSONDecodeError:
                if attempt < _READ_RETRY_ATTEMPTS - 1:
                    time.sleep(_READ_RETRY_DELAY_S)
        return _empty_envelope(None)

    def _read_other_device_envelopes(self, target, password, own_device_id):
        smb_scanner.register(target, password)
        other_devices = {}
        for entry in smb_scanner.scandir(target):
            if not entry.is_file():
                continue
            match = self._device_file_re.match(entry.name)
            if not match or match.group(1) == own_device_id:
                continue
            other_devices[match.group(1)] = self._read_device_envelope(target, entry.name)
        return other_devices

    # ---- remoto: escritura (SOLO el archivo propio) ----

    def _write_remote_envelope(self, target, password, filename, envelope):
        # NO escritura "atómica" vía rename/replace — se probó (ver
        # CONTEXT.md Paso 34) y smbclient.replace() falla contra el
        # servidor Samba real del usuario. Mitigación del lado de la
        # LECTURA (_read_device_envelope, reintenta ante JSONDecodeError)
        # — acá aplica mejor que nunca: cada archivo tiene un único
        # escritor posible (este dispositivo), así que la ventana de
        # colisión es mucho más rara que en el diseño viejo (cualquier
        # dispositivo podía escribir el mismo archivo compartido en
        # cualquier momento).
        smb_scanner.register(target, password)
        with smb_scanner.open_text(target, filename, mode="w") as f:
            json.dump(envelope, f, indent=2, ensure_ascii=False)

    # ---- API pública, consumida por progress_store.py/track_prefs.py ----

    def set_entry(self, key, value, updated_at=None):
        """SIEMPRE local, síncrono — nunca dispara push por sí sola. Sirve
        igual para bibliotecas centralizadas (la key ya viene prefijada
        con el uid compartido, ver centralized_store.key_prefix) o no
        (prefijada con library.id): el mismo envelope local contiene
        ambas, sin distinción — lo único que cambia es si alguna vez se
        sube a algún target remoto (ver push())."""
        updated_at = time.time() if updated_at is None else updated_at

        def _mutate(envelope):
            envelope["entries"][key] = {**value, "updated_at": updated_at}
            envelope["tombstones"].pop(key, None)
            return envelope

        self._mutate_local(_mutate)

    def delete_entry(self, key, updated_at=None):
        """Tombstone puntual — ej. episodio que superó FINISHED_THRESHOLD.
        Necesario (no alcanza con borrar de `entries` sin más) para que el
        merge de lectura no lo resucite si otro dispositivo todavía tiene
        una copia vieja sin borrar de esta misma clave."""
        updated_at = time.time() if updated_at is None else updated_at

        def _mutate(envelope):
            envelope["entries"].pop(key, None)
            envelope["tombstones"][key] = updated_at
            return envelope

        self._mutate_local(_mutate)

    def delete_prefix(self, prefix, updated_at=None):
        """"Marcar serie como vista" (progress_store.delete_series_entries)
        — agrega un tombstone de PREFIJO, que el merge de lectura aplica
        contra entradas de CUALQUIER dispositivo (ver
        merge_device_payloads), no solo las que este conoce. El caller
        típicamente dispara un push() (async) después de esto."""
        updated_at = time.time() if updated_at is None else updated_at

        def _mutate(envelope):
            envelope["entries"] = {
                k: v for k, v in envelope["entries"].items() if not k.startswith(prefix)
            }
            envelope["tombstones"] = {
                k: v for k, v in envelope["tombstones"].items() if not k.startswith(prefix)
            }
            envelope["prefix_tombstones"].append({"prefix": prefix, "updated_at": updated_at})
            return envelope

        self._mutate_local(_mutate)

    def delete_prefix_local_only(self, prefix):
        """Purga DURA, sin tombstone — usado al borrar una biblioteca de
        ESTA app (ver progress_store.delete_library_entries): no debe
        afectar a otras instalaciones que sigan usando el mismo progreso
        compartido (lo que promete el diálogo de confirmación: "no borra
        ni modifica nada en el share real"). Un tombstone SÍ viajaría en
        el próximo push y le diría a los demás "esto se borró" — acá la
        intención es "olvidate vos, no le avises a nadie", así que ni
        siquiera se genera uno."""

        def _mutate(envelope):
            envelope["entries"] = {
                k: v for k, v in envelope["entries"].items() if not k.startswith(prefix)
            }
            envelope["tombstones"] = {
                k: v for k, v in envelope["tombstones"].items() if not k.startswith(prefix)
            }
            envelope["prefix_tombstones"] = [
                pt for pt in envelope["prefix_tombstones"] if pt["prefix"] != prefix
            ]
            return envelope

        self._mutate_local(_mutate)

    def get(self, library):
        """Lectura fusionada, nunca bloquea I/O de red (todo desde
        memoria + el envelope local, lectura de disco chica y barata): el
        envelope LOCAL fresco de este dispositivo (nunca la última copia
        remota conocida DE SÍ MISMO, que puede estar un paso atrás de un
        push todavía en vuelo) + la cache de "otros dispositivos" poblada
        por preload(). Sin centralizar: solo el local. Centralizada pero
        todavía no precargada/no disponible: fusiona igual con lo que
        haya en cache (vacío la primera vez) — mismo criterio que
        _items_cache.get() en gallery.py, "no aparece todavía"."""
        target = target_for_library(library)
        if target is None:
            return self._get_local_only()
        own_envelope = self._load_local_envelope()
        with self._lock:
            cached = self._remote_cache.get(target_key(target))
        other_envelopes = list(cached["other_devices"].values()) if cached else []
        return merge_device_payloads(other_envelopes + [own_envelope])

    def preload(self, library):
        """Llamado DESDE UN QThread ya existente (ScanWorker/
        ScanEpisodesWorker) — nunca crea su propio thread. No-op si no
        está centralizada o si el target ya está listo en cache (ver
        invalidate() para forzar una relectura)."""
        target = target_for_library(library)
        if target is None:
            return
        key = target_key(target)
        with self._lock:
            if self._remote_cache.get(key, {}).get("state") == "ready":
                return
        password = progress_password(library)
        own_device_id = self._own_device_id()
        try:
            other_devices = self._read_other_device_envelopes(target, password, own_device_id)
            state = "ready"
        except smb_scanner.StorageError:
            other_devices, state = {}, "unavailable"
        with self._lock:
            self._remote_cache[key] = {"other_devices": other_devices, "state": state}
        notifier.offline_changed.emit(state == "unavailable", target_label(library))

    def invalidate(self, library):
        """Tira la cache de "otros dispositivos" de este target para que
        el próximo preload() vuelva a leer del share — así se ve el
        progreso que se guardó desde otra máquina. A diferencia del
        diseño viejo, ya NO hace falta ningún guard de "dirty": toda
        mutación local (set_entry/delete_entry/delete_prefix) ya quedó
        escrita a DISCO antes de tocar esta cache — nunca existe un
        estado "solo en memoria" que una relectura pueda pisar."""
        target = target_for_library(library)
        if target is None:
            return
        with self._lock:
            self._remote_cache.pop(target_key(target), None)

    def iter_all_entries(self, libraries):
        """Recorre TODAS las entradas relevantes para un conjunto de
        bibliotecas: el store local UNA sola vez (contiene las entradas de
        TODAS las bibliotecas no centralizadas juntas) + get() de cada
        target remoto DISTINTO entre las centralizadas (una vez por
        target, no por biblioteca — varias pueden compartir el mismo
        archivo remoto, ver target_key). Yields (key, entry)."""
        seen_targets = set()
        has_local = False
        for library in libraries:
            target = target_for_library(library)
            if target is None:
                has_local = True
                continue
            key = target_key(target)
            if key in seen_targets:
                continue
            seen_targets.add(key)
            yield from self.get(library).items()
        if has_local:
            yield from self._get_local_only().items()

    def push(self, library, blocking=False, timeout_ms=3000):
        """Sube el envelope LOCAL completo al archivo remoto PROPIO de
        este dispositivo — nunca al de otro (ver comentario del módulo).
        No-op si la biblioteca no centraliza.

        blocking=False (pausa): programa el push en un QThread de fondo y
        vuelve al toque — pausar tiene que sentirse instantáneo.
        blocking=True ("Volver", cierre de la app): programa (si hace
        falta) y ESPERA con tope a que termine, incluyendo un eventual
        "rerun" encadenado (ver wait_pending)."""
        target = target_for_library(library)
        if target is None:
            return
        self._schedule_push(library, target)
        if blocking:
            self.wait_pending(library, timeout_ms)

    def _schedule_push(self, library, target):
        key = target_key(target)
        with self._lock:
            if key in self._flushing:
                # Ya hay un push en vuelo para este target — a diferencia
                # del diseño viejo (que confiaba en el timer remoto de 5s
                # para reintentar solo), acá ya NO hay ningún timer de
                # respaldo, así que no alcanza con descartar este pedido:
                # se guarda como "correr uno más al terminar", con un
                # snapshot FRESCO tomado recién en ESE momento (no ahora,
                # que ya quedaría desactualizado apenas termine el actual).
                self._pending_rerun[key] = library
                return
            self._flushing.add(key)
            snapshot = self._load_local_envelope()
        # password (keyring/KDE Wallet) se resuelve DENTRO del worker, no
        # acá — _schedule_push corre en el hilo LLAMADOR, que para
        # blocking=False (pausa) es el hilo de Qt: una consulta a keyring
        # sincrónica acá adentro le pega directo a "pausar tiene que
        # sentirse instantáneo" (medido en vivo: ~55ms, nada catastrófico
        # pero nada gratis tampoco). El resto del snapshot (I/O de disco
        # local chico) sí se queda acá, es sub-milisegundo.
        filename = f"{self._remote_prefix}.{self._own_device_id()}.json"
        worker = _PushWorker(self, library, target, key, filename, snapshot)
        worker.finished.connect(lambda: self._on_push_done(key, worker))
        with self._lock:
            self._flush_workers.append(worker)
        worker.start()

    def _on_push_done(self, key, worker):
        """Puede llegar a correr DOS veces para el mismo worker: la
        entrega real de la señal `finished` (conexión QUEUED, ver
        _wait_pending_key) Y una llamada síncrona manual desde ahí mismo.
        El guard `_handled` hace que la segunda sea un no-op — sin él, la
        segunda invocación podría hacer `_flushing.discard(key)` sobre un
        push NUEVO (un rerun ya arrancado por la primera invocación),
        dejando `_flushing` en un estado incorrecto."""
        with self._lock:
            if worker._handled:
                return
            worker._handled = True
            self._flushing.discard(key)
            if worker in self._flush_workers:
                self._flush_workers.remove(worker)
            rerun_library = self._pending_rerun.pop(key, None)
        notifier.offline_changed.emit(not worker.succeeded, target_label(worker.library))
        if rerun_library is not None:
            self._schedule_push(rerun_library, worker.target)

    def wait_pending(self, library, timeout_ms=3000):
        """Bloquea (con tope) hasta que no quede NINGÚN push en vuelo
        para el target de esta biblioteca — usado al volver del
        reproductor o cerrar la app, para darle al último guardado una
        chance real de llegar al share. Espera la cadena COMPLETA (worker
        actual + un eventual rerun encadenado, ver _schedule_push) dentro
        del mismo presupuesto de tiempo — no alcanza con esperar solo al
        primero si "Volver" llegó mientras el push de una pausa anterior
        seguía en curso."""
        target = target_for_library(library)
        if target is None:
            return
        self._wait_pending_key(target_key(target), timeout_ms)

    def wait_all_pending(self, timeout_ms=3000):
        """Como wait_pending(), pero para TODOS los targets con algo en
        vuelo (worker corriendo o rerun pendiente) — usado al cerrar la
        app, donde puede haber más de una biblioteca centralizada con un
        push disparado hace instantes."""
        with self._lock:
            keys = {w.target_key for w in self._flush_workers} | set(self._pending_rerun)
        for key in keys:
            self._wait_pending_key(key, timeout_ms)

    def _wait_pending_key(self, key, timeout_ms):
        """No puede confiar en que la señal `finished` ya haya disparado
        _on_push_done para decidir que un worker terminó: esa conexión es
        QUEUED (lambda sin afinidad de hilo propia), y su entrega necesita
        que ALGÚN event loop la procese — si quien llama a esto es el
        hilo de Qt (el caso real: "Volver"/cierre de la app), ESE es
        justo el hilo que está bloqueado adentro de este mismo método,
        así que la señal no tiene forma de entregarse hasta que
        volvamos. Confirmado en vivo con un test aislado: sin este fix,
        cada espera consumía el timeout ENTERO (busy-loop) aunque el
        push ya hubiera terminado hace rato.

        Por eso, apenas worker.wait() confirma que el hilo terminó de
        verdad, se dispara _on_push_done() A MANO (síncrono) en vez de
        esperar a que la entrega encolada llegue sola — así, si eso
        encadena un rerun (ver _schedule_push), el próximo chequeo de
        `workers` ya lo ve. La entrega real de la señal, si llega más
        tarde, es inofensiva (ver el guard `_handled` en _on_push_done)."""
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            with self._lock:
                workers = [w for w in self._flush_workers if w.target_key == key]
            if not workers:
                return
            remaining_ms = max(0, round((deadline - time.monotonic()) * 1000))
            worker = workers[0]  # como mucho uno en vuelo por key a la vez, ver _schedule_push
            finished = worker.wait(remaining_ms)
            if finished:
                self._on_push_done(key, worker)
            if not finished or time.monotonic() >= deadline:
                return


class _PushWorker(QThread):
    def __init__(self, store, library, target, key, filename, snapshot):
        super().__init__()
        self.store = store
        self.library = library
        self.target = target
        self.target_key = key
        self.snapshot = snapshot
        self.succeeded = False
        self._filename = filename
        self._handled = False  # ver el guard en RemoteJsonStore._on_push_done

    def run(self):
        try:
            # password acá adentro (no en _schedule_push) — ver el
            # comentario ahí: mismo criterio que preload()/ScanWorker, la
            # consulta a keyring corre en ESTE hilo de fondo, nunca en el
            # de Qt.
            password = progress_password(self.library)
            self.store._write_remote_envelope(self.target, password, self._filename, self.snapshot)
        except Exception:
            self.succeeded = False
        else:
            self.succeeded = True


# ---- migración (activar centralización con progreso local ya existente) ----
#
# Mucho más simple que el diseño viejo: como cada dispositivo escribe
# SOLO su propio archivo remoto, ya no existe ningún documento compartido
# que decidir "pisar" o "respetar" (las opciones viejas "usar los del
# servidor"/"sobreescribir con los de esta PC" no tienen sentido acá — no
# hay conflicto posible, nunca hay dos escritores del mismo archivo). Lo
# único que queda por decidir es si migrar el progreso LOCAL de esta
# biblioteca (guardado bajo `library.id`, de cuando todavía no
# centralizaba) al prefijo del uid compartido, para que el próximo push()
# lo suba y quede visible para los demás dispositivos.


def analyze_migration_for_store(store, local_prefix):
    """Cuánto progreso LOCAL hay para esta biblioteca, bajo el prefijo
    viejo (library.id) — para que el diálogo pueda preguntar "¿migrar tu
    progreso local?" antes de activar centralización. Puramente local, no
    hace falta tocar la red para esto (a diferencia del diseño viejo, que
    necesitaba leer el remoto para saber si "ya había datos ahí")."""
    local_envelope = store._load_local_envelope()
    local_count = sum(1 for k in local_envelope["entries"] if k.startswith(local_prefix))
    return {"local_count": local_count}


def apply_migration_for_store(store, local_prefix, remote_prefix, migrate):
    """migrate=True: rekeyea las entradas LOCALES de esta biblioteca
    (guardadas bajo local_prefix=library.id) al prefijo del uid
    COMPARTIDO (remote_prefix) — sin este rekey quedarían en un
    compartimento que ninguna otra instalación puede encontrar, que era
    el bug de diseño original. El próximo push() (lo dispara el caller
    después de esto) las sube bajo la clave correcta.
    migrate=False: no hace nada — el progreso local sigue bajo
    library.id, invisible para la centralización de acá en más (no se
    borra, solo deja de usarse para esta biblioteca)."""
    if not migrate:
        return

    def _mutate(envelope):
        for key in list(envelope["entries"]):
            if key.startswith(local_prefix):
                new_key = remote_prefix + key[len(local_prefix):]
                envelope["entries"][new_key] = envelope["entries"].pop(key)
        for key in list(envelope["tombstones"]):
            if key.startswith(local_prefix):
                new_key = remote_prefix + key[len(local_prefix):]
                envelope["tombstones"][new_key] = envelope["tombstones"].pop(key)
        return envelope

    store._mutate_local(_mutate)
