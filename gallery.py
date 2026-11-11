"""
UI de la galería: selector de biblioteca + grilla de posters, y el diálogo
para agregar una biblioteca nueva (server/share SMB + tipo series/movies).

El scanning y las pruebas de conexión corren en QThread aparte: son
llamadas de red (SMB) y no deben congelar la ventana principal.
"""

from pathlib import Path

from PySide6.QtCore import Qt, QThread, QVariantAnimation, Signal
from PySide6.QtGui import QFont, QFontMetrics, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGraphicsDropShadowEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

import app_restart
import app_settings
import centralized_store
import library_store
import credentials
import library_metadata
import progress_store
import smb_scanner
import theme
import track_prefs
import track_probe
from library_store import Library

POSTER_W, POSTER_H = 150, 220
POSTER_RADIUS = 8
GRID_COLUMNS = 5
HOVER_ZOOM = 1.06
HOVER_ANIM_MS = 150
LOGO_SIZE = 36
CONTINUE_WATCHING_LIMIT = 15


def has_resolvable_progress(library_by_id):
    """A diferencia de progress_store.get_all_progress() a secas, esto
    chequea que la entrada apunte a una biblioteca que TODAVÍA existe —
    progress.json puede tener entradas huérfanas (ej. se borró y se
    volvió a agregar una biblioteca; Library.new() siempre genera un id
    nuevo, nunca reusa el viejo) que jamás van a poder resolverse en
    resolve_continue_watching_entries(). Sin este chequeo la tab quedaba
    visible pero vacía en ese caso.

    Función de módulo (no método de GalleryView) a propósito: es lógica
    pura sobre datos ya en memoria, sin tocar ningún widget — la reusa
    tal cual TVGalleryView (tv_gallery.py), en vez de reimplementar la
    misma resolución con riesgo de reintroducir el mismo bug en un
    segundo lugar (ver Paso 26, Ronda 3 en CONTEXT.md)."""
    return any(
        library_id in library_by_id
        for library_id, _subpath, _pos, _dur, _updated_at
        in progress_store.get_all_progress(list(library_by_id.values()))
    )


def resolve_continue_watching_entries(library_by_id, items_cache):
    """[{"library", "name", "poster_bytes", "fraction", "is_movie",
    "series_subpath", "episode_subpath"}, ...] — entradas de
    progress_store resueltas contra lo que ya está en items_cache (sin
    I/O nuevo). Entradas cuya biblioteca todavía no terminó de escanearse
    esta sesión simplemente no aparecen todavía — se resuelven solas
    cuando termine ese scan.

    Una sola entrada por SERIE (no por episodio): si hay progreso
    guardado en varios episodios de la misma serie (se vio el 1, se
    saltó al 2, se abrió el 7 más tarde...) solo se muestra el más
    reciente. get_all_progress() ya viene ordenado por más reciente
    primero, así que alcanza con quedarse con la primera ocurrencia de
    cada (library_id, series_subpath) y descartar el resto — no hace
    falta comparar número de episodio.

    Función de módulo, mismo motivo que has_resolvable_progress() arriba
    — reusada tal cual por TVGalleryView."""
    results = []
    seen_series = set()
    all_progress = progress_store.get_all_progress(list(library_by_id.values()))
    for library_id, subpath, position_ms, duration_ms, _updated_at in all_progress:
        if len(results) >= CONTINUE_WATCHING_LIMIT:
            break
        library = library_by_id.get(library_id)
        if not library:
            continue  # biblioteca borrada desde que se guardó este progreso
        cached_items = items_cache.get(library_id)
        if cached_items is None:
            continue

        # Series: el progreso es por EPISODIO (subpath completo), pero
        # la carátula/nombre que ya tenemos cacheados son de la SERIE
        # (subpath de un solo componente, la carpeta raíz) — hay que
        # buscar por ese subpath más corto, no por el del episodio.
        is_movie = library.type == "movies"
        series_subpath = subpath if is_movie else (subpath[0],)
        series_key = (library_id, series_subpath)
        if series_key in seen_series:
            continue
        match = next((entry for entry in cached_items if entry[1] == series_subpath), None)
        if not match:
            continue
        seen_series.add(series_key)
        name, _sp, poster_bytes, _raw_name, _sort_key = match
        results.append({
            "library": library,
            "name": name,
            "poster_bytes": poster_bytes,
            "fraction": position_ms / duration_ms if duration_ms else None,
            "is_movie": is_movie,
            "series_subpath": series_subpath,
            "episode_subpath": subpath,
        })
    return results


def patch_series_name(items_cache, library, subpath, new_name):
    """Actualiza (mutación in-place) el nombre mostrado de la serie en
    `subpath` dentro de `items_cache[library.id]`, sin re-escanear SMB —
    usado tras un rename exitoso en la vista de episodios (desktop) o al
    resolver el nombre real de una serie con metadata custom (TV, que no
    permite renombrar pero sí tiene que MOSTRAR el nombre resuelto). True
    si efectivamente cambió algo, para que el caller sepa si hace falta
    re-renderizar la grilla.

    Función de módulo, mismo motivo que has_resolvable_progress/
    resolve_continue_watching_entries — reusada tal cual por
    TVGalleryView.update_series_name."""
    items = items_cache.get(library.id)
    if items is None:
        return False
    for i, (name, sp, poster_bytes, raw_name, sort_key) in enumerate(items):
        if sp == subpath:
            if name != new_name:
                items[i] = (new_name, sp, poster_bytes, raw_name, sort_key)
                return True
            return False
    return False


def _normalize_search(text):
    """minúsculas y sin espacios — así "Die Buster"/"diebuster"/"Die  Buster"
    matchean entre sí (pedido explícito: buscar con y sin espacios)."""
    return "".join(text.lower().split())


def matches_search_query(query, name, raw_name, season_names):
    """query ya viene normalizado (_normalize_search). Matchea nombre
    mostrado, nombre crudo de carpeta/archivo, O el nombre de cualquier
    temporada custom de esa serie (ej. buscar "Gun Buster" encuentra "Aim
    for the Top!" si esa es la temporada 1) — pedido explícito del
    usuario, no solo el nombre de la serie.

    Función de módulo, mismo motivo que has_resolvable_progress/
    resolve_continue_watching_entries/patch_series_name — reusada tal
    cual por TVGalleryView (tv_gallery.py)."""
    if query in _normalize_search(name) or query in _normalize_search(raw_name):
        return True
    return any(query in _normalize_search(season_name) for season_name in season_names)


def _elide_two_lines(text, font, max_width):
    """Los nombres de release de fansub son gigantes (tags de grupo, BD,
    resolución, codec...) — un word-wrap sin límite de altura desborda la
    tarjeta y pisa el poster de abajo. Se arma manualmente una primera
    línea que entra en max_width, y una segunda con elidedText si hace
    falta, midiendo con QFontMetrics (más preciso que contar caracteres,
    porque el ancho real depende de qué letras son)."""
    metrics = QFontMetrics(font)
    words = text.split()
    line1_words = []
    i = 0
    while i < len(words):
        candidate = " ".join(line1_words + [words[i]])
        if line1_words and metrics.horizontalAdvance(candidate) > max_width:
            break
        line1_words.append(words[i])
        i += 1

    if i >= len(words):
        return " ".join(line1_words)

    line2 = " ".join(words[i:])
    line2 = metrics.elidedText(line2, Qt.TextElideMode.ElideRight, max_width)
    return f"{' '.join(line1_words)}\n{line2}"


def _rounded_rect_path(w, h, radius, corners="all"):
    """corners='all': las 4 esquinas parejas (uso general). corners='top':
    solo las de ARRIBA redondeadas, abajo cuadradas — para que la carátula
    empalme sin costura con la franja de título pegada debajo (ver
    PosterTile), mismo truco que usa el reproductor para el video pegado
    a la barra de controles (main.py._update_video_mask)."""
    path = QPainterPath()
    if corners == "all":
        path.addRoundedRect(0, 0, w, h, radius, radius)
        return path
    path.moveTo(0, h)
    path.lineTo(0, radius)
    path.quadTo(0, 0, radius, 0)
    path.lineTo(w - radius, 0)
    path.quadTo(w, 0, w, radius)
    path.lineTo(w, h)
    path.closeSubpath()
    return path


def _rounded_pixmap(pixmap, radius, corners="all"):
    """QSS border-radius no recorta el contenido de un QPixmap en un
    QLabel (limitación conocida de Qt) — hay que recortarlo a mano con un
    QPainterPath para que el poster realmente tenga esquinas redondeadas.
    El borde de hover NO se dibuja acá — vive en el QWidget contenedor
    (PosterTile.card), ver ahí por qué alcanza con un border-radius normal
    por QSS en ese caso (el contenedor no tiene contenido de pixmap
    propio, solo el borde)."""
    result = QPixmap(pixmap.size())
    result.fill(Qt.GlobalColor.transparent)
    painter = QPainter(result)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setClipPath(_rounded_rect_path(pixmap.width(), pixmap.height(), radius, corners))
    painter.drawPixmap(0, 0, pixmap)
    painter.end()
    return result


def _zoomed_pixmap(source_pixmap, width, height, zoom, radius, corners="all"):
    """Recorta el pixmap a exactamente width x height, mostrando una
    porción más 'acercada' del original según zoom (1.0 = normal, >1.0 =
    zoom in) — el equivalente Qt de 'overflow: hidden' + 'transform:
    scale()' en CSS. El marco (poster_label) nunca cambia de tamaño, así
    que agrandar en hover no reacomoda el resto de la grilla."""
    scaled_w = max(1, round(width * zoom))
    scaled_h = max(1, round(height * zoom))
    scaled = source_pixmap.scaled(
        scaled_w, scaled_h,
        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation,
    )
    x = max(0, (scaled.width() - width) // 2)
    y = max(0, (scaled.height() - height) // 2)
    cropped = scaled.copy(x, y, width, height)
    return _rounded_pixmap(cropped, radius, corners=corners)


class ConnectionTestWorker(QThread):
    succeeded = Signal()
    failed = Signal(str)

    def __init__(self, library, password):
        super().__init__()
        self.library = library
        self.password = password

    def run(self):
        try:
            smb_scanner.test_connection(self.library, self.password)
        except smb_scanner.StorageError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:
            self.failed.emit(f"Error inesperado: {exc}")
        else:
            self.succeeded.emit()


class LibraryTrackProbeWorker(QThread):
    """Sondea las pistas de audio/subtítulo reales (ver track_probe.py) de
    CADA serie de una biblioteca recién agregada, de una sola vez — pedido
    explícito del usuario: mejor pagar ese costo una vez al agregar la
    biblioteca que ir goteándolo la primera vez que se abre cada serie
    (que es como funcionaba antes, ver episodes.py ScanEpisodesWorker).
    No aplica a bibliotecas de películas (no tienen concepto de serie)."""

    progress = Signal(int, int, str)  # (series ya procesadas, total, nombre de la serie actual)
    finished_ok = Signal()

    def __init__(self, library, password):
        super().__init__()
        self.library = library
        self.password = password

    def run(self):
        try:
            entries = smb_scanner.scan_series(self.library, self.password)
        except Exception:
            # Si ni se puede listar la biblioteca no hay nada para
            # sondear — la conexión ya se probó antes de llegar acá
            # (botón "Probar conexión"), así que esto sería un caso raro.
            self.finished_ok.emit()
            return

        total = len(entries)
        for i, entry in enumerate(entries, start=1):
            self.progress.emit(i, total, entry.name)
            try:
                # get_track_probe (lectura) va DENTRO del try — puede
                # relanzar StorageError (ver library_metadata._read_json)
                # ante un hipo de red real, no solo cuando el sidecar
                # genuinamente no tiene nada sondeado todavía. Antes vivía
                # afuera del try, sin capturar nada — un hipo acá tumbaba
                # el QThread entero sin terminar de sondear el resto de la
                # biblioteca, y sin emitir finished_ok nunca (bug real
                # encontrado en vivo: "Actualizar biblioteca" corría el
                # escaneo de ítems bien, pero el sondeo de pistas nunca
                # aparecía).
                if library_metadata.get_track_probe(self.library, self.password, entry.name) is not None:
                    continue  # ya sondeada — no debería pasar recién agregada, pero por las dudas
                groups = smb_scanner.scan_episodes(self.library, self.password, entry.subpath)
                probe_subpath = track_probe.pick_probe_subpath(groups)
                if probe_subpath is None:
                    continue
                audio_descriptions, subtitle_descriptions = track_probe.probe_tracks(
                    self.library, self.password, probe_subpath
                )
                library_metadata.set_track_probe(
                    self.library, self.password, entry.name,
                    {"audio": audio_descriptions, "subtitle": subtitle_descriptions},
                )
            except Exception:
                # Una serie puntual que falla (timeout, archivo corrupto,
                # etc.) no debe frenar el resto — se reintenta sola la
                # próxima vez que se abra esa serie (mismo fallback de
                # siempre en episodes.py).
                continue
        self.finished_ok.emit()


class ProgressRefreshWorker(QThread):
    """Relee el progreso/prefs centralizados de las bibliotecas que
    centralizan, para que "Continuar viendo" muestre lo que se vio desde
    OTRA máquina. Corre al volver a la galería (ver
    GalleryView.refresh_continue_watching): es una lectura chica de un
    JSON, mucho más barata que un ScanWorker completo (que además baja
    posters), pero igual va en un QThread porque puede pegarle a la red.
    No-op si ninguna biblioteca centraliza."""

    refreshed = Signal()

    def __init__(self, libraries):
        super().__init__()
        self.libraries = libraries

    def run(self):
        did_something = False
        for library in self.libraries:
            if not getattr(library, "progress_centralized", False):
                continue
            password = credentials.get_password(library.id, library.username) or ""
            centralized_store.resolve_library_uid(library, password)
            progress_store.refresh(library)
            track_prefs.refresh(library)
            did_something = True
        if did_something:
            self.refreshed.emit()


class ProgressMigrationAnalysisWorker(QThread):
    """Antes de activar (o cambiar el target de) la centralización de
    progreso/prefs de una biblioteca, mide cuánto progreso LOCAL hay para
    migrar — así el diálogo puede preguntarle al usuario si migrarlo,
    ANTES de guardar nada (ver AddLibraryDialog.accept()).

    Diseño "un archivo por dispositivo" (ver centralized_store.py): ya no
    hace falta leer nada REMOTO acá — no existe ningún documento
    compartido cuyo estado importe (cada dispositivo escribe solo el
    suyo, nunca hay nada que "ya esté ahí" en el sentido del diseño
    viejo). Sigue haciendo falta resolver el uid compartido ANTES de
    guardar, como chequeo de que el share de la biblioteca es alcanzable
    — mejor frenar acá que guardar una biblioteca centralizada con un
    target sin identidad resuelta."""

    analyzed = Signal(object)  # {"progress": {"local_count":int}, "track_prefs": {...}}
    failed = Signal(str)

    def __init__(self, library, library_password):
        super().__init__()
        self.library = library
        self.library_password = library_password

    def run(self):
        try:
            uid = centralized_store.resolve_library_uid(self.library, self.library_password)
            if uid is None:
                self.failed.emit(
                    "No se pudo leer la identidad de la biblioteca "
                    "(lunyx-metadata.json) — revisá la conexión de la biblioteca."
                )
                return
            plan = {
                "progress": progress_store.analyze_migration(self.library),
                "track_prefs": track_prefs.analyze_migration(self.library),
            }
        except smb_scanner.StorageError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:
            self.failed.emit(f"Error inesperado: {exc}")
        else:
            self.analyzed.emit(plan)


class ProgressMigrationApplyWorker(QThread):
    """Ejecuta la decisión que el usuario ya tomó (ver
    AddLibraryDialog._on_migration_analyzed): migrar o no el progreso
    local existente de esta biblioteca al prefijo del uid compartido.

    Si se migró algo, dispara un push BLOQUEANTE (con tope) al final —
    para que quede visible para otros dispositivos apenas se cierre este
    diálogo, en vez de esperar al próximo checkpoint natural (pausa/
    Volver), que podría tardar si el usuario ni siquiera está
    reproduciendo nada todavía. Corre en un QThread, así que bloquear acá
    adentro no traba la UI."""

    finished_ok = Signal()
    failed = Signal(str)

    def __init__(self, library, library_password, migrate):
        super().__init__()
        self.library = library
        self.library_password = library_password
        self.migrate = migrate

    def run(self):
        try:
            uid = centralized_store.resolve_library_uid(self.library, self.library_password)
            if uid is None:
                self.failed.emit("No se pudo leer la identidad de la biblioteca (lunyx-metadata.json).")
                return
            progress_store.apply_migration(self.library, uid, self.migrate)
            track_prefs.apply_migration(self.library, uid, self.migrate)
            if self.migrate:
                progress_store.checkpoint(self.library, blocking=True)
                track_prefs.checkpoint(self.library, blocking=True)
        except Exception as exc:
            self.failed.emit(f"No se pudo aplicar la migración: {exc}")
        else:
            self.finished_ok.emit()


class ConnectionFieldsWidget:
    """Bloque reusable de campos de conexión SMB/local: combo tipo +
    server/share/subcarpeta/usuario/contraseña, o carpeta local +
    "Examinar...", con el toggle `QFormLayout.setRowVisible` ya armado
    (gallery.py históricamente lo tenía inline en AddLibraryDialog — se
    extrae acá porque hace falta el mismo bloque DOS veces: la conexión de
    la biblioteca, y la conexión opcional de progreso centralizado). No es
    un QWidget — es un agrupador de widgets sueltos que se insertan como
    filas de un QFormLayout externo vía add_rows_to(); no necesita layout
    propio."""

    def __init__(self, path_placeholder="Anime (opcional, subcarpeta dentro del share)",
                 local_placeholder="/home/usuario/Anime"):
        self.connection_type_input = QComboBox()
        self.connection_type_input.addItem("SMB (red)", "smb")
        self.connection_type_input.addItem("Carpeta local", "local")
        self.connection_type_input.currentIndexChanged.connect(self._update_fields)

        self.server_input = QLineEdit()
        self.server_input.setPlaceholderText("192.168.1.50 o nombre-del-servidor")
        self.share_input = QLineEdit()
        self.share_input.setPlaceholderText("media")
        self.path_input = QLineEdit()
        self.path_input.setPlaceholderText(path_placeholder)
        self.username_input = QLineEdit()
        self.password_input = QLineEdit()
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)

        self.local_path_input = QLineEdit()
        self.local_path_input.setPlaceholderText(local_placeholder)
        browse_button = QPushButton("Examinar...")
        browse_button.clicked.connect(self._browse_local_path)
        local_path_row = QHBoxLayout()
        local_path_row.setContentsMargins(0, 0, 0, 0)
        local_path_row.addWidget(self.local_path_input, stretch=1)
        local_path_row.addWidget(browse_button)
        self.local_path_container = QWidget()
        self.local_path_container.setLayout(local_path_row)

        self._form = None  # seteado recién en add_rows_to

    def add_rows_to(self, form, conn_label="Conexión:", server_label="Servidor:",
                     share_label="Share:", path_label="Subcarpeta:", username_label="Usuario:",
                     password_label="Contraseña:", local_label="Carpeta:"):
        self._form = form
        form.addRow(conn_label, self.connection_type_input)
        form.addRow(server_label, self.server_input)
        form.addRow(share_label, self.share_input)
        form.addRow(path_label, self.path_input)
        form.addRow(username_label, self.username_input)
        form.addRow(password_label, self.password_input)
        form.addRow(local_label, self.local_path_container)
        self._update_fields()

    def is_local(self):
        return self.connection_type_input.currentData() == "local"

    def _update_fields(self):
        if self._form is None:
            return
        is_local = self.is_local()
        for row_widget in (self.server_input, self.share_input, self.path_input, self.username_input, self.password_input):
            self._form.setRowVisible(row_widget, not is_local)
        self._form.setRowVisible(self.local_path_container, is_local)

    def _browse_local_path(self):
        start_dir = self.local_path_input.text().strip() or str(Path.home())
        directory = QFileDialog.getExistingDirectory(None, "Elegir carpeta", start_dir)
        if directory:
            self.local_path_input.setText(directory)

    def load_from(self, connection_type, server, share, path, username):
        conn_index = self.connection_type_input.findData(connection_type)
        if conn_index >= 0:
            self.connection_type_input.setCurrentIndex(conn_index)
        if connection_type == "local":
            self.local_path_input.setText(path)
        else:
            self.server_input.setText(server)
            self.share_input.setText(share)
            self.path_input.setText(path)
            self.username_input.setText(username)
        self._update_fields()

    def build_kwargs(self):
        """dict {connection_type/server/share/path/username} — nombres
        listos para pasarle directo a Library(...)/Library.new(...) o a
        centralized_store.StorageTarget(...) vía **kwargs."""
        if self.is_local():
            return {"connection_type": "local", "server": "", "share": "", "path": self.local_path_input.text().strip(), "username": ""}
        return {
            "connection_type": "smb",
            "server": self.server_input.text().strip(),
            "share": self.share_input.text().strip(),
            "path": self.path_input.text().strip(),
            "username": self.username_input.text().strip(),
        }

    def password_text(self):
        return self.password_input.text()

    def set_password_placeholder(self, text):
        self.password_input.setPlaceholderText(text)

    def has_minimum_fields(self):
        kwargs = self.build_kwargs()
        if kwargs["connection_type"] == "local":
            return bool(kwargs["path"])
        return bool(kwargs["server"] and kwargs["share"])


class AddLibraryDialog(QDialog):
    """Mismo diálogo para agregar Y editar: si se pasa 'library', arranca
    precargado con sus datos y agrega el botón "Eliminar biblioteca". La
    contraseña nunca se precarga (no se lee de vuelta del keyring) —
    dejarla vacía al guardar significa "no cambiarla", excepto si se
    cambió el usuario (ver accept())."""

    def __init__(self, parent=None, library=None):
        super().__init__(parent)
        self._editing_library = library
        self.setWindowTitle("Editar biblioteca" if library else "Agregar biblioteca")
        self.setMinimumWidth(440)
        self._test_worker = None
        self._progress_test_worker = None
        self._probe_worker = None
        self._migration_analysis_worker = None
        self._migration_apply_worker = None
        # Estado "en vuelo" de un accept() mientras corre la migración —
        # ver _start_migration_analysis/_finish_save_library.
        self._pending_library = None
        self._pending_password = None
        self._pending_library_password = None
        self._pending_progress_password = None
        self._pending_username_changed = False
        self._pending_progress_username_changed = False
        self._pending_is_new_series_library = False

        # Snapshot del target de progreso tal como estaba guardado ANTES
        # de abrir el diálogo — se compara contra lo que el usuario deja
        # tildado/completado al aceptar, para saber si hace falta correr
        # el flujo de migración (ver _progress_target_changed).
        if library:
            self._original_progress_state = (
                library.progress_centralized, library.progress_connection_type, library.progress_server,
                library.progress_share, library.progress_path, library.progress_username,
            )
        else:
            self._original_progress_state = (False, "local", "", "", "", "")

        self.name_input = QLineEdit()
        self.type_input = QComboBox()
        self.type_input.addItem("Series", "series")
        self.type_input.addItem("Películas", "movies")

        self._library_conn = ConnectionFieldsWidget()
        self._progress_conn = ConnectionFieldsWidget(
            path_placeholder="(opcional, subcarpeta dentro del share)",
            local_placeholder="/home/usuario/lunyx-progreso",
        )
        self.progress_centralized_checkbox = QCheckBox(
            "Centralizar progreso y preferencias de pista en un servidor"
        )
        # Atajo pedido explícito: evita tener que volver a tipear
        # server/share/subcarpeta/usuario/contraseña cuando lo que se
        # quiere es centralizar en la MISMA carpeta donde ya vive
        # lunyx-metadata.json — un solo click copia la conexión de la
        # biblioteca (ya cargada arriba en el formulario) a la de
        # progreso. Sigue quedando la opción de vieja de elegir una
        # carpeta distinta a mano, esto es un ATAJO, no reemplaza nada.
        self.use_library_connection_button = QPushButton(
            "Usar la misma carpeta que la biblioteca"
        )
        self.use_library_connection_button.clicked.connect(self._use_library_connection_for_progress)

        # self.form tiene que existir ANTES de tocar el checkbox/combos con
        # datos precargados más abajo — el toggle de "centralizar" y el de
        # SMB/local reaccionan a sus propias señales tocando self.form
        # (setRowVisible), así que precargarlos antes de que el form exista
        # dispararía esas señales sobre un atributo todavía inexistente.
        self.form = QFormLayout()
        self.form.setSpacing(10)
        self.form.addRow("Nombre:", self.name_input)
        self.form.addRow("Tipo:", self.type_input)
        self._library_conn.add_rows_to(self.form)
        self.form.addRow(self.progress_centralized_checkbox)
        self.form.addRow(self.use_library_connection_button)
        self._progress_conn.add_rows_to(
            self.form,
            conn_label="Conexión (progreso):", server_label="Servidor (progreso):",
            share_label="Share (progreso):", path_label="Subcarpeta (progreso):",
            username_label="Usuario (progreso):", password_label="Contraseña (progreso):",
            local_label="Carpeta (progreso):",
        )
        self.progress_test_button = QPushButton("Probar conexión de progreso")
        self.progress_test_button.clicked.connect(self._test_progress_connection)
        self.progress_centralized_checkbox.toggled.connect(self._update_progress_fields_visibility)

        if library:
            self.name_input.setText(library.name)
            type_index = self.type_input.findData(library.type)
            if type_index >= 0:
                self.type_input.setCurrentIndex(type_index)
            self._library_conn.load_from(
                library.connection_type, library.server, library.share, library.path, library.username
            )
            self._library_conn.set_password_placeholder("(dejar vacío para no cambiar)")
            if library.progress_centralized:
                self.progress_centralized_checkbox.setChecked(True)
                self._progress_conn.load_from(
                    library.progress_connection_type, library.progress_server,
                    library.progress_share, library.progress_path, library.progress_username,
                )
                self._progress_conn.set_password_placeholder("(dejar vacío para no cambiar)")

        self.status_label = QLabel("")
        self.test_button = QPushButton("Probar conexión")
        self.test_button.clicked.connect(self._test_connection)

        self._update_progress_fields_visibility()  # también deja server/share/local del progreso bien ocultos/mostrados

        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)

        self.status_label.setStyleSheet("color: #8a8f99; font-size: 12px;")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.setSpacing(12)
        layout.addLayout(self.form)
        layout.addWidget(self.test_button)
        layout.addWidget(self.progress_test_button)
        if library:
            delete_button = QPushButton("Eliminar biblioteca")
            delete_button.setObjectName("dangerButton")
            delete_button.clicked.connect(self._delete_library)
            layout.addWidget(delete_button)
        layout.addWidget(self.status_label)
        layout.addWidget(self.button_box)

    def _update_progress_fields_visibility(self):
        checked = self.progress_centralized_checkbox.isChecked()
        self._progress_conn._update_fields()  # aplica el toggle SMB/local normal del bloque
        rows = (
            self._progress_conn.connection_type_input, self._progress_conn.server_input,
            self._progress_conn.share_input, self._progress_conn.path_input,
            self._progress_conn.username_input, self._progress_conn.password_input,
            self._progress_conn.local_path_container,
        )
        if not checked:
            for row_widget in rows:
                self.form.setRowVisible(row_widget, False)
        else:
            self.form.setRowVisible(self._progress_conn.connection_type_input, True)
        self.progress_test_button.setVisible(checked)

    def _use_library_connection_for_progress(self):
        """Handler de "Usar la misma carpeta que la biblioteca" — copia
        connection_type/server/share/subcarpeta/usuario/contraseña del
        bloque de la biblioteca (arriba en el formulario) al de progreso,
        y prende el checkbox de centralizar si todavía no estaba (así las
        filas de progreso quedan visibles con los datos ya puestos, sin
        pasos extra)."""
        kwargs = self._library_conn.build_kwargs()
        self._progress_conn.load_from(
            kwargs["connection_type"], kwargs["server"], kwargs["share"],
            kwargs["path"], kwargs["username"],
        )
        self._progress_conn.password_input.setText(self._library_conn.password_text())
        if not self.progress_centralized_checkbox.isChecked():
            self.progress_centralized_checkbox.setChecked(True)

    def _build_library(self):
        name = self.name_input.text().strip()
        type_ = self.type_input.currentData()
        conn_kwargs = self._library_conn.build_kwargs()
        progress_kwargs = self._build_progress_kwargs()
        if self._editing_library:
            return Library(id=self._editing_library.id, name=name, type=type_, **conn_kwargs, **progress_kwargs)
        return Library.new(name=name, type_=type_, **conn_kwargs, **progress_kwargs)

    def _build_progress_kwargs(self):
        if not self.progress_centralized_checkbox.isChecked():
            return {"progress_centralized": False}
        conn = self._progress_conn.build_kwargs()
        return {
            "progress_centralized": True,
            "progress_connection_type": conn["connection_type"],
            "progress_server": conn["server"],
            "progress_share": conn["share"],
            "progress_path": conn["path"],
            "progress_username": conn["username"],
        }

    def _test_connection(self):
        library = self._build_library()
        password = self._library_conn.password_text()
        if not password and self._editing_library:
            password = credentials.get_password(self._editing_library.id, self._editing_library.username) or ""
        if not (library.name and self._library_conn.has_minimum_fields()):
            self.status_label.setText(
                "Completá al menos nombre y carpeta." if library.connection_type == "local"
                else "Completá al menos nombre, servidor y share."
            )
            return

        self.test_button.setEnabled(False)
        self.status_label.setText("Probando conexión..." if library.connection_type == "smb" else "Probando carpeta...")

        self._test_worker = ConnectionTestWorker(library, password)
        self._test_worker.succeeded.connect(self._on_test_ok)
        self._test_worker.failed.connect(self._on_test_failed)
        self._test_worker.start()

    def _on_test_ok(self):
        self.test_button.setEnabled(True)
        self.status_label.setText("Conexión OK.")

    def _on_test_failed(self, message):
        self.test_button.setEnabled(True)
        self.status_label.setText(message)

    def _test_progress_connection(self):
        if not self._progress_conn.has_minimum_fields():
            self.status_label.setText(
                "Completá al menos la carpeta de progreso." if self._progress_conn.is_local()
                else "Completá al menos servidor y share de progreso."
            )
            return
        target = centralized_store.StorageTarget(**self._progress_conn.build_kwargs())
        password = self._progress_conn.password_text()
        if not password and self._editing_library and self._editing_library.progress_centralized:
            password = centralized_store.progress_password(self._editing_library)

        self.progress_test_button.setEnabled(False)
        self.status_label.setText(
            "Probando conexión de progreso..." if target.connection_type == "smb" else "Probando carpeta de progreso..."
        )

        self._progress_test_worker = ConnectionTestWorker(target, password)
        self._progress_test_worker.succeeded.connect(self._on_progress_test_ok)
        self._progress_test_worker.failed.connect(self._on_progress_test_failed)
        self._progress_test_worker.start()

    def _on_progress_test_ok(self):
        self.progress_test_button.setEnabled(True)
        self.status_label.setText("Conexión de progreso OK.")

    def _on_progress_test_failed(self, message):
        self.progress_test_button.setEnabled(True)
        self.status_label.setText(message)

    def _progress_target_changed(self, library):
        """True si se activó centralización recién ahora, o si el target
        (conexión completa) cambió respecto al que ya tenía guardado —
        en cualquiera de los dos casos hay que resolver el flujo de
        migración antes de guardar. Apagar la centralización (pasa a
        False) NO dispara nada acá — simplemente se guarda así, sin tocar
        el archivo remoto viejo."""
        if not library.progress_centralized:
            return False
        current = (
            library.progress_centralized, library.progress_connection_type, library.progress_server,
            library.progress_share, library.progress_path, library.progress_username,
        )
        return current != self._original_progress_state

    def accept(self):
        library = self._build_library()
        if library.connection_type == "local":
            if not (library.name and library.path):
                QMessageBox.warning(self, "Faltan datos", "Completá nombre y carpeta.")
                return
        elif not (library.name and library.server and library.share and library.username):
            QMessageBox.warning(self, "Faltan datos", "Completá nombre, servidor, share y usuario.")
            return

        if library.progress_centralized:
            if library.progress_connection_type == "local":
                if not library.progress_path:
                    QMessageBox.warning(self, "Faltan datos", "Completá la carpeta de progreso.")
                    return
            elif not (library.progress_server and library.progress_share and library.progress_username):
                QMessageBox.warning(
                    self, "Faltan datos",
                    "Completá servidor, share y usuario de la conexión de progreso.",
                )
                return

        password = self._library_conn.password_text()
        username_changed = (
            library.connection_type == "smb"
            and self._editing_library and self._editing_library.username != library.username
        )
        if username_changed and not password:
            QMessageBox.warning(
                self, "Falta la contraseña",
                "Cambiaste el usuario — hace falta que ingreses la contraseña de nuevo.",
            )
            return

        progress_password_value = self._progress_conn.password_text()
        progress_username_changed = (
            library.progress_centralized and library.progress_connection_type == "smb"
            and self._editing_library and self._editing_library.progress_username != library.progress_username
        )
        if progress_username_changed and not progress_password_value:
            QMessageBox.warning(
                self, "Falta la contraseña",
                "Cambiaste el usuario de la conexión de progreso — hace falta que ingreses la contraseña de nuevo.",
            )
            return

        # Antes de tocar library_store, para poder distinguir "biblioteca
        # de series recién agregada" (dispara el sondeo masivo de pistas
        # más abajo) de una edición (no dispara nada).
        is_new_series_library = not self._editing_library and library.type == "series"

        self._pending_library = library
        self._pending_password = password
        self._pending_progress_password = progress_password_value
        self._pending_username_changed = username_changed
        self._pending_progress_username_changed = progress_username_changed
        self._pending_is_new_series_library = is_new_series_library

        if self._progress_target_changed(library):
            # Se activó centralización por primera vez, o se cambió el
            # target respecto al guardado — hay que resolver qué hacer con
            # datos ya existentes ANTES de guardar nada: si el usuario
            # cancela acá, no debe quedar guardada una biblioteca con
            # progress_centralized=True apuntando a un target sin resolver.
            self._start_migration_analysis(library, password, progress_password_value)
            return

        self._finish_save_library()

    def _set_busy(self, busy, status_text=""):
        self.form.setEnabled(not busy)
        self.test_button.setEnabled(not busy)
        self.progress_test_button.setEnabled(not busy)
        self.button_box.setEnabled(not busy)
        self.status_label.setText(status_text)

    def _start_migration_analysis(self, library, library_password, progress_password_value):
        # La contraseña de la BIBLIOTECA hace falta aparte de la del target
        # de progreso: el uid compartido vive en el sidecar de la
        # biblioteca (ver centralized_store.resolve_library_uid). Si el
        # campo quedó vacío en modo edición, se recupera del keyring.
        if not library_password and self._editing_library:
            library_password = credentials.get_password(
                self._editing_library.id, self._editing_library.username
            ) or ""
        self._pending_library_password = library_password

        # La contraseña de PROGRESO se guarda en keyring ACÁ, antes de
        # analizar/aplicar la migración — no recién en _finish_save_library
        # como el resto de los cambios (que se confirman todos juntos al
        # final). Motivo: si se migra algo, ProgressMigrationApplyWorker
        # dispara un push real (checkpoint) contra el target de progreso,
        # y push() resuelve la contraseña desde keyring (ver
        # centralized_store.progress_password) — para una conexión de
        # progreso RECIÉN tipeada (nunca guardada antes), ese push
        # fallaría por credencial vacía si no se guarda antes. Costo
        # aceptado: si el usuario cancela el diálogo completo después de
        # esto, queda una credencial huérfana en el keyring bajo un
        # library.id que nunca se terminó de guardar — inocuo, mismo tipo
        # de imperfección menor ya aceptada en otros lugares del proyecto.
        if progress_password_value and library.progress_centralized and library.progress_connection_type == "smb":
            centralized_store.set_progress_password(library, progress_password_value)

        self._set_busy(True, "Analizando datos existentes...")
        self._migration_analysis_worker = ProgressMigrationAnalysisWorker(library, library_password)
        self._migration_analysis_worker.analyzed.connect(self._on_migration_analyzed)
        self._migration_analysis_worker.failed.connect(self._on_migration_failed)
        self._migration_analysis_worker.start()

    def _on_migration_failed(self, message):
        self._set_busy(False)
        self._pending_library = None
        QMessageBox.warning(
            self, "No se pudo usar la carpeta de progreso",
            f"No se pudo leer la carpeta de progreso elegida: {message}\n\nNo se guardó ningún cambio.",
        )

    def _on_migration_analyzed(self, plan):
        # Con el diseño "un archivo por dispositivo" (ver
        # centralized_store.py) ya no existe la pregunta "¿el servidor ya
        # tiene datos, cuál gana?" — cada dispositivo escribe SOLO su
        # propio archivo remoto, nunca hay dos escritores posibles del
        # mismo archivo, así que no hay ningún conflicto que resolver. Lo
        # único que queda por decidir es si migrar el progreso LOCAL de
        # esta biblioteca — una sola pregunta combinada (progreso +
        # preferencias de pista migran juntos, no tiene sentido separarlos
        # para el usuario).
        total_local = plan["progress"]["local_count"] + plan["track_prefs"]["local_count"]
        migrate = False
        if total_local > 0:
            confirm = QMessageBox.question(
                self, "Migrar progreso local",
                f"Esta biblioteca tiene progreso/preferencias guardadas localmente en "
                f"esta PC ({total_local} entradas). ¿Migrarlas a la centralización recién "
                "activada, para que se vean también desde otros equipos?\n\n"
                "(Lo local no se borra, solo deja de usarse para esta biblioteca de acá en más.)",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            migrate = confirm == QMessageBox.StandardButton.Yes

        self.status_label.setText("Aplicando migración..." if migrate else "Guardando...")
        self._migration_apply_worker = ProgressMigrationApplyWorker(
            self._pending_library, self._pending_library_password, migrate,
        )
        self._migration_apply_worker.finished_ok.connect(self._finish_save_library)
        self._migration_apply_worker.failed.connect(self._on_migration_failed)
        self._migration_apply_worker.start()

    def _finish_save_library(self):
        library = self._pending_library
        password = self._pending_password
        progress_password_value = self._pending_progress_password
        username_changed = self._pending_username_changed
        progress_username_changed = self._pending_progress_username_changed
        is_new_series_library = self._pending_is_new_series_library

        if self._editing_library:
            if username_changed:
                credentials.delete_password(self._editing_library.id, self._editing_library.username)
            if progress_username_changed:
                centralized_store.delete_progress_password(self._editing_library)
            library_store.update_library(library)
        else:
            library_store.add_library(library)
        if password and library.connection_type == "smb":
            credentials.set_password(library.id, library.username, password)
        if progress_password_value and library.progress_centralized and library.progress_connection_type == "smb":
            centralized_store.set_progress_password(library, progress_password_value)

        if is_new_series_library:
            # No cierra el diálogo todavía — sondear pistas de cada serie
            # (ver LibraryTrackProbeWorker) puede tardar, así que se
            # muestra el progreso acá mismo y recién se cierra al
            # terminar (_on_probe_done llama a super().accept()).
            self._set_busy(True, "Sondeando pistas de audio/subtítulo...")
            self._probe_worker = LibraryTrackProbeWorker(library, password)
            self._probe_worker.progress.connect(self._on_probe_progress)
            self._probe_worker.finished_ok.connect(self._on_probe_done)
            self._probe_worker.start()
            return

        super().accept()

    def _on_probe_progress(self, done, total, series_name):
        self.status_label.setText(
            f"Sondeando pistas de audio/subtítulo... ({done}/{total}) {series_name}"
        )

    def _on_probe_done(self):
        super().accept()

    def _delete_library(self):
        confirm = QMessageBox.question(
            self, "Eliminar biblioteca",
            f"¿Eliminar '{self._editing_library.name}'?\n\n"
            "Esto solo quita la biblioteca de esta app (configuración local "
            "y contraseña guardada) — no borra ni modifica nada en el share real.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        library_store.remove_library(self._editing_library.id)
        credentials.delete_password(self._editing_library.id, self._editing_library.username)
        centralized_store.delete_progress_password(self._editing_library)
        centralized_store.forget_library_uid(self._editing_library.id)
        library_metadata.clear_cache(self._editing_library.id)
        # Sin esto quedan huérfanas en progress.json/track_prefs.json,
        # apuntando a un library_id que ya no existe — causaba que
        # "Continuar viendo" quedara visible pero vacía si se borraba y
        # volvía a agregar la misma biblioteca (Library.new() genera un
        # id nuevo cada vez, nunca reusa el viejo). Si la biblioteca
        # centralizaba, esto es best-effort contra el target remoto: si no
        # está alcanzable en este momento no bloquea el borrado, solo deja
        # esas entradas huérfanas allá (mismo trade-off aceptado que el
        # resto de la centralización, ver centralized_store.py).
        progress_store.delete_library_entries(self._editing_library)
        track_prefs.delete_library_entries(self._editing_library)
        self.done(QDialog.DialogCode.Accepted)


GITHUB_URL = "https://github.com/antarest47/lunyx"


def _read_app_version():
    try:
        return (Path(__file__).parent / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "?"


class AboutDialog(QDialog):
    """"Acerca de" — logo, versión (lee VERSION, el mismo archivo que usa
    el empaquetado, ver flatpak/io.github.antarest47.Lunyx.metainfo.xml)
    y link al repo. Nada de créditos ni changelog acá, solo identificar
    qué versión es — accesible desde "Gestionar bibliotecas"."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Acerca de Lunyx")
        self.setFixedWidth(320)

        logo_size = 96
        logo_label = QLabel()
        logo_label.setFixedSize(logo_size, logo_size)
        logo_pixmap = QPixmap(str(Path(__file__).parent / "logo.png"))
        if not logo_pixmap.isNull():
            logo_label.setPixmap(logo_pixmap.scaled(
                logo_size, logo_size,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            ))

        name_label = QLabel("Lunyx")
        name_label.setStyleSheet(f"font-size: 20px; font-weight: 600; color: {theme.TEXT};")
        version_label = QLabel(f"Versión {_read_app_version()}")
        version_label.setStyleSheet(f"color: {theme.TEXT_DIM};")

        link_label = QLabel(f'<a href="{GITHUB_URL}" style="color: {theme.ACCENT};">{GITHUB_URL}</a>')
        link_label.setOpenExternalLinks(True)
        link_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        link_label.setCursor(Qt.CursorShape.PointingHandCursor)

        close_button = QPushButton("Cerrar")
        close_button.clicked.connect(self.accept)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 20)
        layout.setSpacing(8)
        layout.addWidget(logo_label, alignment=Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(name_label, alignment=Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(version_label, alignment=Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(link_label, alignment=Qt.AlignmentFlag.AlignHCenter)
        layout.addSpacing(8)
        layout.addWidget(close_button)


class ManageLibrariesDialog(QDialog):
    """Lista todas las bibliotecas configuradas con un botón "Editar" cada
    una (abre AddLibraryDialog en modo edición, con su propio botón
    "Eliminar" adentro — ver ahí) + un botón para agregar una nueva.
    Reemplaza al lápiz que antes vivía al lado de cada tab — un solo punto
    de entrada, más prolijo visualmente."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Gestionar bibliotecas")
        self.setMinimumWidth(420)
        self.changed = False  # GalleryView lo chequea al cerrar para saber si hace falta reload_libraries()

        self.title_input = QLineEdit(app_settings.get_collection_title())
        self._title_save_button = QPushButton("Guardar")
        self._title_save_button.clicked.connect(self._save_title)

        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        title_row.addWidget(QLabel("Nombre de la colección:"))
        title_row.addWidget(self.title_input, stretch=1)
        title_row.addWidget(self._title_save_button)

        self.list_layout = QVBoxLayout()
        self.list_layout.setSpacing(8)

        self._add_button = QPushButton("+ Agregar biblioteca nueva")
        self._add_button.clicked.connect(self._add_library)

        # Mismo diálogo, mismas funciones, en los dos modos — el texto
        # refleja a qué modo se pasaría SI se lo aprieta (no el modo
        # actual), como cualquier toggle. Vive acá (no como un botón
        # aparte más visible) a propósito: cambiar de modo va a ser algo
        # poco frecuente, no debe competir con lo que se usa todo el
        # tiempo (ver CONTEXT.md, iniciativa "modo TV").
        switch_mode_label = (
            "Volver a modo Desktop" if app_settings.get_ui_mode() == "tv" else "Cambiar a modo TV"
        )
        switch_mode_button = QPushButton(switch_mode_label)
        switch_mode_button.clicked.connect(self._switch_mode)

        about_button = QPushButton("Acerca de")
        about_button.clicked.connect(self._show_about)

        close_button = QPushButton("Cerrar")
        close_button.clicked.connect(self.accept)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.setSpacing(12)
        layout.addLayout(title_row)
        layout.addLayout(self.list_layout)
        layout.addWidget(self._add_button)
        layout.addWidget(switch_mode_button)
        layout.addWidget(about_button)
        layout.addWidget(close_button)

        self._reload_list()

    def _save_title(self):
        # No hace falta un flag de "cambió" aparte: GalleryView relee
        # app_settings.get_collection_title() incondicionalmente al
        # cerrar este diálogo (ver _open_manage_dialog), es barato.
        app_settings.set_collection_title(self.title_input.text())

    def _show_about(self):
        AboutDialog(self).exec()

    def _reload_list(self):
        while self.list_layout.count():
            item = self.list_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

        libraries = library_store.list_libraries()
        if not libraries:
            empty_label = QLabel("No hay bibliotecas configuradas todavía.")
            empty_label.setStyleSheet(f"color: {theme.TEXT_DIM};")
            self.list_layout.addWidget(empty_label)
            QWidget.setTabOrder(self._title_save_button, self._add_button)
            return

        # Tab-order explícito (bug real reportado en vivo, modo TV): estos
        # botones "Editar" se crean acá, DESPUÉS de add/switch/close (ya
        # existen desde __init__, y _reload_list() se vuelve a llamar cada
        # vez que se agrega/edita una biblioteca) — Qt arma la cadena de
        # foco por defecto según el orden de creación de los hijos, no por
        # posición visual en el layout, así que sin esto "Editar" siempre
        # terminaba al FINAL de la cadena (después de "Cerrar"), nunca
        # entre "Guardar" y "+ Agregar biblioteca nueva" como se ve en
        # pantalla.
        type_labels = {"series": "Series", "movies": "Películas"}
        previous = self._title_save_button
        for library in libraries:
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(10)
            info_label = QLabel(f"{library.name}  ·  {type_labels.get(library.type, library.type)}")
            edit_button = QPushButton("Editar")
            edit_button.clicked.connect(lambda checked, lib=library: self._edit_library(lib))
            row_layout.addWidget(info_label, stretch=1)
            row_layout.addWidget(edit_button)
            self.list_layout.addWidget(row)
            QWidget.setTabOrder(previous, edit_button)
            previous = edit_button
        QWidget.setTabOrder(previous, self._add_button)

    def _add_library(self):
        dialog = AddLibraryDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.changed = True
            self._reload_list()

    def _edit_library(self, library):
        dialog = AddLibraryDialog(self, library=library)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.changed = True
            self._reload_list()

    def _switch_mode(self):
        new_mode = "desktop" if app_settings.get_ui_mode() == "tv" else "tv"
        app_restart.request_restart(new_mode)


class ScanWorker(QThread):
    # season_search: {subpath: [nombre_de_temporada, ...]} — solo series
    # con nombres de temporada custom guardados (ver
    # library_metadata.get_all_season_names), para que el buscador de la
    # galería encuentre la serie por el nombre de una temporada.
    # OJO: el segundo parámetro tiene que ser "object", NO "dict" — PySide6
    # convierte un parámetro de Signal declarado "dict" a QVariantMap
    # internamente, que solo admite keys de tipo string. subpath es una
    # tupla, así que con "dict" el diccionario llegaba VACÍO al otro lado
    # de la señal (confirmado con un test aislado) sin ningún error visible
    # — "object" pasa el dict de Python tal cual, sin conversión.
    finished_ok = Signal(list, object)
    failed = Signal(str)

    def __init__(self, library):
        super().__init__()
        self.library = library

    def run(self):
        password = credentials.get_password(self.library.id, self.library.username) or ""

        # El uid compartido se resuelve ANTES de precargar: es el prefijo
        # con el que se guardan/leen las entradas de una biblioteca
        # centralizada (ver centralized_store.resolve_library_uid). Usa la
        # contraseña de la BIBLIOTECA (el sidecar vive ahí), no la del
        # target de progreso. Precargar la cache de progreso/prefs es
        # no-op si esta biblioteca no centraliza — así el shelf "Continuar
        # viendo" (get_all_progress) y la vista de episodios lo encuentran
        # cacheado sin bloquear el hilo de Qt.
        centralized_store.resolve_library_uid(self.library, password)
        progress_store.preload(self.library)
        track_prefs.preload(self.library)

        try:
            if self.library.type == "movies":
                entries = smb_scanner.scan_movies(self.library, password)
            else:
                entries = smb_scanner.scan_series(self.library, password)

            items = []
            season_search = {}
            for entry in entries:
                poster_bytes = smb_scanner.fetch_poster_bytes(self.library, entry.poster_subpath)
                # entry.name (nombre de carpeta/archivo) sigue siendo la KEY
                # estable para todo lo demás (progreso, prefs, metadata) —
                # acá solo se pisa lo que se MUESTRA.
                if self.library.type == "movies":
                    display_name = library_metadata.get_movie_name(
                        self.library, password, entry.name, fallback=entry.name
                    )
                    # sort_key: nombre de ordenación invisible (ver
                    # library_metadata.get_raw_movie_sort_key) — cae al
                    # display_name si nunca se seteó uno custom, mismo
                    # orden alfabético de siempre.
                    sort_key = library_metadata.get_movie_sort_key(
                        self.library, password, entry.name, fallback=display_name
                    )
                else:
                    display_name = library_metadata.get_series_name(
                        self.library, password, entry.name, fallback=entry.name
                    )
                    sort_key = display_name  # las series no tienen este campo, sin cambio de comportamiento
                    season_names = library_metadata.get_all_season_names(
                        self.library, password, entry.name
                    )
                    if season_names:
                        season_search[entry.subpath] = season_names
                items.append((display_name, entry.subpath, poster_bytes, entry.name, sort_key))
        except smb_scanner.StorageError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:
            self.failed.emit(f"Error inesperado escaneando: {exc}")
        else:
            self.finished_ok.emit(items, season_search)


class UpdateLibraryDialog(QDialog):
    """Diálogo BLOQUEANTE para "Actualizar biblioteca" — pedido explícito
    del usuario: antes, actualizar corría el re-escaneo de ítems Y el
    sondeo de pistas (ver LibraryTrackProbeWorker) en background, sin
    ningún indicador — "no lo quiero corriendo en el fondo, quiero que
    termine el sondeo para poder seguir". Mismo criterio visual que
    AddLibraryDialog al agregar una biblioteca de series nueva (progreso
    "Sondeando pistas... (3/21) Nombre", UI bloqueada hasta terminar) —
    acá directamente TODO el diálogo es ese estado, sin formulario.

    Corre el rescan de ítems (ScanWorker) Y el sondeo de pistas
    (LibraryTrackProbeWorker, salvo para bibliotecas de películas) en
    paralelo, y no se cierra solo hasta que los DOS terminan — así el
    caller (GalleryView/TVGalleryView) recibe el resultado final ya
    completo en una sola señal, en vez de tener que coordinar dos workers
    sueltos. Reusado tal cual por TV, mismo patrón que ManageLibrariesDialog/
    AddLibraryDialog (TV nunca reimplementa un diálogo, solo le agrega
    navegación por flechas si hace falta)."""

    finished_all = Signal(list, object)  # (items, season_search) — mismo formato que ScanWorker.finished_ok

    def __init__(self, library, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Actualizar biblioteca")
        self.setModal(True)
        self._library = library
        self._items_result = None
        self._season_search_result = None
        self._probe_done = library.type == "movies"  # películas no tienen sondeo de pistas

        self.status_label = QLabel(f"Actualizando {library.name}...")
        self.status_label.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.addWidget(self.status_label)
        self.setFixedWidth(440)

        # Mismo motivo que _refresh_current_library ya tenía: nombres/
        # temporadas custom cacheados en memoria de una sesión previa no
        # se invalidan solos.
        library_metadata.clear_cache(library.id)
        password = credentials.get_password(library.id, library.username) or ""

        self._scan_worker = ScanWorker(library)
        self._scan_worker.finished_ok.connect(self._on_scan_ok)
        self._scan_worker.failed.connect(self._on_scan_failed)
        self._scan_worker.start()

        if library.type != "movies":
            self._probe_worker = LibraryTrackProbeWorker(library, password)
            self._probe_worker.progress.connect(self._on_probe_progress)
            self._probe_worker.finished_ok.connect(self._on_probe_done)
            self._probe_worker.start()

    def _on_scan_ok(self, items, season_search):
        self._items_result = items
        self._season_search_result = season_search
        self._maybe_finish()

    def _on_scan_failed(self, message):
        self._items_result = []
        self._season_search_result = {}
        self.status_label.setText(message)
        self._maybe_finish()

    def _on_probe_progress(self, done, total, series_name):
        self.status_label.setText(
            f"Sondeando pistas de audio/subtítulo... ({done}/{total}) {series_name}"
        )

    def _on_probe_done(self):
        self._probe_done = True
        self._maybe_finish()

    def _maybe_finish(self):
        if self._items_result is not None and self._probe_done:
            self.finished_all.emit(self._items_result, self._season_search_result)
            self.accept()


class PosterTile(QWidget):
    def __init__(self, name, pixmap, on_click, badge_text=None, progress_fraction=None):
        super().__init__()
        self._on_click = on_click
        self._source_pixmap = pixmap if (pixmap and not pixmap.isNull()) else None
        self._zoom = 1.0
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(6, 6, 6, 6)
        outer_layout.setSpacing(0)

        # card: UN solo rectángulo con esquinas redondeadas (carátula +
        # franja de título, pegadas sin espacio) — mismo truco que el
        # reproductor (video + barra de controles, ver
        # main.py PlayerView.player_card): la carátula se recorta con las
        # esquinas de ARRIBA redondeadas (_rounded_pixmap corners="top"),
        # la franja de abajo tiene las de ABAJO redondeadas por QSS
        # (border-bottom-*-radius, funciona bien acá porque no tiene
        # contenido de pixmap, solo texto+color de fondo) — el contorno
        # combinado de las dos da un rectángulo con las 4 esquinas
        # parejas, así que el borde de hover en "card" (QSS normal,
        # border-radius parejo) coincide exacto con ese contorno sin
        # necesidad de dibujarlo a mano.
        self.card = QWidget()
        self.card.setObjectName("posterCard")
        self.card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._card_style_base = (
            f"QWidget#posterCard {{ border: 2px solid transparent; "
            f"border-radius: {POSTER_RADIUS}px; background: transparent; }}"
        )
        self._card_style_hover = (
            f"QWidget#posterCard {{ border: 2px solid {theme.ACCENT}; "
            f"border-radius: {POSTER_RADIUS}px; background: transparent; }}"
        )
        self.card.setStyleSheet(self._card_style_base)

        card_layout = QVBoxLayout(self.card)
        # 2px = ancho del borde de "card" (ver _card_style_base/_hover más
        # abajo) — con margen 0 la carátula+título ocupaban TODO el
        # rectángulo de card y tapaban su propio borde (los hijos se
        # pintan encima del padre). Con este margen queda un hueco fijo
        # donde el borde (transparente en reposo, ámbar en hover) es
        # visible en vez de estar tapado.
        card_layout.setContentsMargins(2, 2, 2, 2)
        card_layout.setSpacing(0)

        self.poster_label = QLabel()
        self.poster_label.setObjectName("posterImage")
        self.poster_label.setFixedSize(POSTER_W, POSTER_H)
        self.poster_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        if self._source_pixmap:
            self.poster_label.setPixmap(
                _zoomed_pixmap(self._source_pixmap, POSTER_W, POSTER_H, 1.0, POSTER_RADIUS, corners="top")
            )
            self.poster_label.setStyleSheet("background: transparent; border: none;")
        else:
            self.poster_label.setText("Sin\nposter")
            self.poster_label.setStyleSheet(
                f"background-color: #23262e; color: #6b7078; "
                f"border-top-left-radius: {POSTER_RADIUS}px; border-top-right-radius: {POSTER_RADIUS}px;"
            )

        if badge_text:
            # Solo aparece en resultados de búsqueda cross-library, para
            # distinguir de qué biblioteca es cada resultado. Hijo directo
            # de poster_label, posicionado a mano (no hay layout que
            # "flote" un widget encima de otro en Qt) — transparente a
            # mouse events para no interceptar el click que abre la serie.
            badge = QLabel(badge_text, self.poster_label)
            badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            badge.setStyleSheet(
                f"background-color: rgba(10, 11, 13, 0.8); color: {theme.ACCENT}; "
                "font-size: 10px; font-weight: 600; border-radius: 4px; padding: 2px 6px;"
            )
            badge.adjustSize()
            badge.move(6, 6)
            badge.raise_()

        if progress_fraction and self._source_pixmap:
            # Barra fina de progreso pegada abajo de la carátula — mismo
            # lenguaje visual que EpisodeRow (episodes.py). Tamaño fijo
            # (no hace falta resizeEvent como en EpisodeRow) porque
            # POSTER_W/POSTER_H nunca cambian, a diferencia del ancho
            # dinámico de una fila de episodio.
            bar_width = round(POSTER_W * max(0.0, min(1.0, progress_fraction)))
            progress_bar = QWidget(self.poster_label)
            progress_bar.setStyleSheet(f"background-color: {theme.ACCENT}; border: none;")
            progress_bar.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            progress_bar.setGeometry(0, POSTER_H - 3, bar_width, 3)
            progress_bar.raise_()

        card_layout.addWidget(self.poster_label)

        name_label = QLabel()
        name_label.setObjectName("posterTitle")
        name_label.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        name_label.setStyleSheet(
            f"background-color: {theme.SURFACE}; color: #c6c9d1; font-size: 12px; "
            f"border-bottom-left-radius: {POSTER_RADIUS}px; border-bottom-right-radius: {POSTER_RADIUS}px; "
            "padding: 6px 4px;"
        )
        measure_font = QFont(name_label.font())
        measure_font.setPixelSize(12)  # tiene que matchear el font-size del QSS de arriba
        name_label.setText(_elide_two_lines(name, measure_font, POSTER_W - 4))
        name_label.setToolTip(name)
        name_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        name_label.setFixedWidth(POSTER_W)
        name_label.setFixedHeight(34 + 12)  # 2 líneas de texto + el padding vertical de arriba
        card_layout.addWidget(name_label)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(24)
        shadow.setOffset(0, 4)
        shadow.setColor(Qt.GlobalColor.black)
        self.card.setGraphicsEffect(shadow)  # sombra de TODA la tarjeta, no solo la carátula

        outer_layout.addWidget(self.card)

        # Hover sutil: borde de acento en toda la tarjeta + zoom leve solo
        # en la carátula (recortado dentro del mismo marco de tamaño fijo,
        # así no reacomoda la grilla — ver _zoomed_pixmap). Animado en vez
        # de instantáneo para que se sienta como "se acerca a la
        # pantalla", no un salto brusco.
        self._zoom_anim = QVariantAnimation(self)
        self._zoom_anim.setDuration(HOVER_ANIM_MS)
        self._zoom_anim.valueChanged.connect(self._apply_zoom)

    def enterEvent(self, event):
        self.card.setStyleSheet(self._card_style_hover)
        if self._source_pixmap:
            self._animate_zoom(HOVER_ZOOM)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.card.setStyleSheet(self._card_style_base)
        if self._source_pixmap:
            self._animate_zoom(1.0)
        super().leaveEvent(event)

    def _animate_zoom(self, target):
        self._zoom_anim.stop()
        self._zoom_anim.setStartValue(self._zoom)
        self._zoom_anim.setEndValue(target)
        self._zoom_anim.start()

    def _apply_zoom(self, value):
        self._zoom = value
        self.poster_label.setPixmap(
            _zoomed_pixmap(self._source_pixmap, POSTER_W, POSTER_H, value, POSTER_RADIUS, corners="top")
        )

    def mousePressEvent(self, event):
        self._on_click()
        super().mousePressEvent(event)


class SearchLineEdit(QLineEdit):
    """QLineEdit que además borra su contenido con Escape (no solo con
    Backspace) — Escape en un QLineEdit normal no hace nada por defecto."""

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape and self.text():
            self.clear()
            return
        super().keyPressEvent(event)


class GalleryView(QWidget):
    """Emite play_requested(library, subpath) cuando el usuario clickea un
    ítem reproducible directamente (películas), o series_requested(library,
    subpath, name) cuando clickea una serie (abre la vista de episodios)."""

    play_requested = Signal(object, tuple, str)
    series_requested = Signal(object, tuple, str)
    # library, series_subpath, episode_subpath, título de fallback —
    # clickear una serie en "Continuar viendo" salta DIRECTO al episodio
    # guardado (no a la lista, ver MainWindow._resume_series_episode).
    resume_episode_requested = Signal(object, tuple, tuple, str)

    def __init__(self):
        super().__init__()
        self._scan_worker = None
        self._progress_refresh_worker = None
        # True mientras se esté esperando a saber si hay progreso en una
        # biblioteca CENTRALIZADA (esa respuesta llega de la red, después
        # de que la galería ya se pintó — ver reload_libraries). Si al
        # llegar resulta que sí hay, se salta a "Continuar viendo", que es
        # la tab por defecto cuando hay algo en progreso.
        self._pending_default_continue_tab = False
        self._current_library = None
        self._current_items = []  # [(name, subpath, poster_bytes, raw_name, sort_key), ...] tal como vino del scan, sin filtrar/ordenar
        self._viewing_continue_watching = False
        self._sort_ascending = True
        # Caché en memoria por biblioteca (library.id -> items) — volver a
        # esta vista (ej. "< Volver a la galería") no debe re-escanear SMB
        # cada vez, solo al seleccionar una biblioteca por primera vez o al
        # pedir "Actualizar biblioteca" explícitamente. Un rename desde la
        # vista de episodios parchea el nombre acá directo (ver
        # update_series_name), tampoco dispara un rescan. También sirve de
        # base para la búsqueda cross-library: se va completando en
        # background para TODAS las bibliotecas configuradas (ver
        # _start_background_prescan), no solo la que está seleccionada.
        self._items_cache = {}
        self._season_search_cache = {}  # library.id -> {subpath: [nombre_temporada, ...]}
        self._library_by_id = {}
        self._tab_buttons = {}
        self._prescan_queue = []
        self._prescan_running = False
        self._background_workers = []

        self.logo_label = QLabel()
        self.logo_label.setFixedSize(LOGO_SIZE, LOGO_SIZE)
        logo_path = Path(__file__).parent / "logo.png"
        logo_pixmap = QPixmap(str(logo_path))
        if not logo_pixmap.isNull():
            scaled = logo_pixmap.scaled(
                LOGO_SIZE, LOGO_SIZE,
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            # Radio = mitad del tamaño -> círculo completo, mismo truco de
            # recorte que _rounded_pixmap usa para los posters.
            self.logo_label.setPixmap(_rounded_pixmap(scaled, LOGO_SIZE // 2))

        self.title_label = QLabel(app_settings.get_collection_title())
        self.title_label.setStyleSheet("font-size: 18px; font-weight: 600; color: #f2f3f5;")

        self.search_input = SearchLineEdit()
        self.search_input.setPlaceholderText("Buscar series o episodios...")
        self.search_input.setMinimumWidth(260)
        self.search_input.textChanged.connect(self._on_search_changed)

        refresh_button = QPushButton("Actualizar biblioteca")
        refresh_button.clicked.connect(self._refresh_current_library)

        self.sort_button = QPushButton("Orden: A-Z")
        self.sort_button.clicked.connect(self._toggle_sort)

        manage_button = QPushButton("Gestionar bibliotecas")
        manage_button.clicked.connect(self._open_manage_dialog)

        title_row = QHBoxLayout()
        title_row.setSpacing(12)
        title_row.addWidget(self.logo_label)
        title_row.addWidget(self.title_label)
        title_row.addWidget(self.search_input, stretch=1)
        title_row.addWidget(refresh_button)
        title_row.addWidget(self.sort_button)
        title_row.addWidget(manage_button)

        # Tabs de biblioteca en vez de dropdown: se reconstruyen en
        # reload_libraries(). addStretch() al final para que los tabs
        # queden pegados a la izquierda en vez de estirarse. Van adentro
        # de tabs_bar (fondo propio, más oscuro que la página) para que
        # se vean como una barra separada, no flotando directo sobre el
        # fondo general — pedido explícito de estilo, con referencia
        # visual de otro proyecto del usuario.
        self.tabs_layout = QHBoxLayout()
        self.tabs_layout.setContentsMargins(10, 8, 10, 8)
        self.tabs_layout.setSpacing(8)
        self.tabs_layout.addStretch()
        self.library_button_group = QButtonGroup(self)
        self.library_button_group.setExclusive(True)

        tabs_bar = QWidget()
        tabs_bar.setObjectName("tabsBar")
        tabs_bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        tabs_bar.setLayout(self.tabs_layout)

        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: #8a8f99; font-size: 12px;")

        self.grid_container = QWidget()
        self.grid_layout = QGridLayout(self.grid_container)
        self.grid_layout.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
        self.grid_layout.setSpacing(4)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.grid_container)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)
        layout.addLayout(title_row)
        layout.addWidget(tabs_bar)
        layout.addWidget(self.status_label)
        layout.addWidget(scroll)

        self.reload_libraries()

    def reload_libraries(self):
        self._clear_tabs()
        libraries = library_store.list_libraries()
        self._library_by_id = {library.id: library for library in libraries}
        self._tab_buttons = {}

        # "Continuar viendo" es la primera tab, antes que cualquier
        # biblioteca real — no corresponde a una Library, así que no
        # entra en self._tab_buttons (que sigue siendo solo library.id ->
        # button) ni dispara ScanWorker por sí sola, usa lo que ya haya
        # en _items_cache.
        self._continue_watching_button = QPushButton("Continuar viendo")
        self._continue_watching_button.setObjectName("libraryTab")
        self._continue_watching_button.setCheckable(True)
        self._continue_watching_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._continue_watching_button.clicked.connect(self._on_continue_watching_tab_selected)
        self.library_button_group.addButton(self._continue_watching_button)
        self.tabs_layout.insertWidget(self.tabs_layout.count() - 1, self._continue_watching_button)
        self._update_continue_watching_tab_visibility()

        for library in libraries:
            button = QPushButton(library.name)
            button.setObjectName("libraryTab")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda checked, lib=library: self._on_library_tab_clicked(lib))
            self.library_button_group.addButton(button)
            self.tabs_layout.insertWidget(self.tabs_layout.count() - 1, button)
            self._tab_buttons[library.id] = button

        # Si hay algo en progreso, "Continuar viendo" es la tab por
        # defecto al abrir la app — si no, se comporta como antes (la
        # primera biblioteca real). Con "Continuar viendo" de entrada
        # ninguna biblioteca recibe un scan en primer plano, así que hay
        # que precargar TODAS en background (antes se salteaba la
        # primera porque ya se escaneaba arriba al seleccionarla).
        if self._has_resolvable_progress() and libraries:
            self._continue_watching_button.setChecked(True)
            self._on_continue_watching_tab_selected()
            self._start_background_prescan(libraries)
        elif libraries:
            # Ojo: para una biblioteca CENTRALIZADA, este chequeo recién
            # corrió contra una cache vacía — su progreso vive en el
            # target remoto y no se lee hasta que corre su ScanWorker (de
            # red, en background). O sea que "no hay progreso" acá todavía
            # no es una respuesta definitiva: se marca como pendiente y se
            # reconcilia cuando termine el scan (ver
            # _reconcile_continue_watching_after_load). Sin esto, la tab
            # "Continuar viendo" no aparecía al abrir la app y recién se
            # asomaba al entrar a una serie y volver.
            self._pending_default_continue_tab = any(
                getattr(lib, "progress_centralized", False) for lib in libraries
            )
            self._tab_buttons[libraries[0].id].setChecked(True)
            self._on_library_selected(libraries[0])
            # Precarga en background el resto de las bibliotecas (la
            # primera ya se está escaneando arriba) para que la búsqueda
            # cross-library tenga datos de todas apenas se abre la app.
            self._start_background_prescan(libraries[1:])
        else:
            self._current_library = None
            self._current_items = []
            self._clear_grid()
            self.status_label.setText("No hay bibliotecas configuradas todavía.")

    def _start_background_prescan(self, libraries):
        # extend(), no reasignar: si esto se llama de nuevo (ej. se agregó
        # una biblioteca nueva) mientras la cola anterior sigue procesando,
        # no hay que arrancar un segundo worker en paralelo — el guard de
        # _prescan_running asegura que el procesamiento siga siendo
        # secuencial, una biblioteca a la vez.
        self._prescan_queue.extend(lib for lib in libraries if lib.id not in self._items_cache)
        if not self._prescan_running:
            self._run_next_prescan()

    def _run_next_prescan(self):
        if not self._prescan_queue:
            self._prescan_running = False
            return
        self._prescan_running = True
        library = self._prescan_queue.pop(0)
        worker = ScanWorker(library)
        worker.finished_ok.connect(
            lambda items, season_search, lib=library: self._on_prescan_ok(lib, items, season_search)
        )
        worker.failed.connect(lambda _msg: self._run_next_prescan())
        self._background_workers.append(worker)
        worker.start()

    def _on_prescan_ok(self, library, items, season_search):
        self._items_cache[library.id] = items
        self._season_search_cache[library.id] = season_search
        if self.search_input.text().strip():
            self._render_grid()
        elif self._viewing_continue_watching:
            # Esta biblioteca puede "destrabar" entradas de progreso que
            # antes no se podían resolver — se reintenta acá, no solo al
            # volver de reproducir algo.
            self._render_grid()
        self._reconcile_continue_watching_after_load()
        self._run_next_prescan()

    def _reconcile_continue_watching_after_load(self):
        """Un scan recién terminado pudo haber traído el progreso de una
        biblioteca centralizada (que al abrir la app todavía no se había
        leído de la red). Recalcula la visibilidad de la tab y, si el
        arranque quedó esperando esa respuesta, salta a "Continuar
        viendo" — la tab por defecto cuando hay algo en progreso."""
        has_progress = self._update_continue_watching_tab_visibility()
        if not self._pending_default_continue_tab:
            return
        # Se chequea la condición REAL, no button.isVisible(): un widget
        # cuya ventana todavía no se mostró devuelve isVisible()==False
        # aunque se le haya hecho setVisible(True), así que usarlo acá
        # haría que este salto nunca ocurriera según el momento del
        # arranque.
        if not has_progress:
            return  # todavía no hay progreso resoluble; puede aparecer con el próximo scan
        self._pending_default_continue_tab = False
        if self.search_input.text().strip():
            return  # el usuario ya está buscando algo, no moverle la vista
        self._continue_watching_button.setChecked(True)
        self._on_continue_watching_tab_selected()

    def _clear_tabs(self):
        for button in self.library_button_group.buttons():
            self.library_button_group.removeButton(button)
        while self.tabs_layout.count() > 1:  # deja el addStretch() final
            item = self.tabs_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

    def _open_manage_dialog(self):
        dialog = ManageLibrariesDialog(self)
        dialog.exec()
        # Barato releer siempre, haya cambiado o no — evita necesitar un
        # segundo flag aparte de "changed" (que es solo para bibliotecas).
        self.title_label.setText(app_settings.get_collection_title())
        if dialog.changed:
            # Cubre agregar, editar Y eliminar — reload_libraries()
            # reconstruye los tabs desde library_store de cero en los tres
            # casos, así que no hace falta distinguirlos acá. Si alguna
            # biblioteca editada/eliminada tenía datos en _items_cache,
            # quedan huérfanos en memoria (inofensivo, no persisten y se
            # pisan solos si se vuelve a agregar una biblioteca con ese id).
            self.reload_libraries()

    def _on_library_tab_clicked(self, library):
        # Solo para clicks REALES del usuario (clicked no se emite con
        # setChecked): elegir una tab a mano cancela el salto automático a
        # "Continuar viendo" que pudiera estar pendiente — si ya decidió
        # dónde quiere estar, moverle la vista de abajo sería molesto.
        self._pending_default_continue_tab = False
        self._on_library_selected(library)

    def _on_library_selected(self, library, force_rescan=False):
        self._current_library = library
        self._viewing_continue_watching = False
        self.search_input.blockSignals(True)
        self.search_input.clear()
        self.search_input.blockSignals(False)

        cached = self._items_cache.get(library.id)
        if cached is not None and not force_rescan:
            self._current_items = cached
            self._render_grid()
            return

        self._current_items = []
        self._clear_grid()
        location = library.path if library.connection_type == "local" else f"{library.server}/{library.share}"
        self.status_label.setText(f"Escaneando {location}...")

        self._scan_worker = ScanWorker(library)
        self._scan_worker.finished_ok.connect(
            lambda items, season_search, lib=library: self._on_scan_ok(lib, items, season_search)
        )
        self._scan_worker.failed.connect(self._on_scan_failed)
        self._scan_worker.start()

    def _refresh_current_library(self):
        """"Actualizar biblioteca" re-escanea los ítems Y re-sondea pistas
        de audio/subtítulo (ver LibraryTrackProbeWorker, Paso 23 — series
        nuevas agregadas a la carpeta SMB desde el último sondeo, o
        bibliotecas que nunca pasaron por uno, quedan al día) — el sondeo
        perezoso on-demand al abrir una serie se SACÓ (ver
        episodes.ScanEpisodesWorker._resolve_track_options) por un crash
        real de libsmbclient al solaparse con una reproducción real, así
        que este es el ÚNICO otro momento en que se sondea, además de
        agregar la biblioteca.

        BLOQUEA la UI con progreso visible hasta que las dos cosas
        terminan (UpdateLibraryDialog, nuevo) — pedido explícito del
        usuario, mismo criterio que ya tenía "Agregar biblioteca": antes
        corría todo en background sin ningún indicador."""
        if not self._current_library:
            return
        library = self._current_library
        dialog = UpdateLibraryDialog(library, self)
        dialog.finished_all.connect(
            lambda items, season_search: self._on_library_updated(library, items, season_search)
        )
        dialog.exec()

    def _on_library_updated(self, library, items, season_search):
        self._items_cache[library.id] = items
        self._season_search_cache[library.id] = season_search
        if self._current_library and self._current_library.id == library.id:
            self._current_items = items
            self._render_grid()

    def update_series_name(self, library, subpath, new_name):
        if patch_series_name(self._items_cache, library, subpath, new_name):
            self._render_grid()

    def _on_search_changed(self, _text):
        self._render_grid()

    def _toggle_sort(self):
        self._sort_ascending = not self._sort_ascending
        self.sort_button.setText(f"Orden: {'A-Z' if self._sort_ascending else 'Z-A'}")
        self._render_grid()

    def _on_scan_ok(self, library, items, season_search):
        # Cachea siempre (el resultado sigue siendo válido para esa
        # biblioteca), pero SOLO pisa _current_items/re-renderiza si esa
        # biblioteca sigue siendo la seleccionada — si el usuario cambió de
        # tab mientras este scan estaba en vuelo, un scan viejo que termina
        # tarde no debe pisar lo que se está mostrando ahora.
        self._items_cache[library.id] = items
        self._season_search_cache[library.id] = season_search
        if self._current_library and self._current_library.id == library.id:
            self._current_items = items
            self._render_grid()
        elif self._viewing_continue_watching:
            # Esta biblioteca puede "destrabar" entradas de progreso que
            # antes no se podían resolver (ver _render_continue_watching_grid).
            self._render_grid()
        self._reconcile_continue_watching_after_load()

    def _switch_active_library(self, library):
        """Cambia el tab activo sin re-escanear (asume que ya está en
        _items_cache) — usado al clickear un resultado de búsqueda que
        pertenece a otra biblioteca que la seleccionada actualmente."""
        self._current_library = library
        self._viewing_continue_watching = False
        self._current_items = self._items_cache.get(library.id, [])
        button = self._tab_buttons.get(library.id)
        if button:
            button.setChecked(True)
        self.search_input.blockSignals(True)
        self.search_input.clear()
        self.search_input.blockSignals(False)
        # No solo cosmético: si no se re-renderiza acá, la grilla se queda
        # mostrando los resultados de búsqueda cross-library hasta la
        # próxima vez que algo dispare _render_grid() — y como ya no hay
        # texto en el buscador, "volver a la galería" mostraría eso mismo
        # sin poder explicarlo (search_input vacío pero grilla de otra
        # búsqueda).
        self._render_grid()

    def _matches_query(self, query, name, raw_name, lib_id, subpath):
        season_names = self._season_search_cache.get(lib_id, {}).get(subpath, ())
        return matches_search_query(query, name, raw_name, season_names)

    def _render_grid(self):
        # Sin búsqueda: solo la biblioteca actual (datos ya en memoria, no
        # vuelve a pegarle al share SMB). Con búsqueda: se busca en TODAS
        # las bibliotecas ya precargadas en _items_cache (ver
        # _start_background_prescan), matcheando nombre mostrado, nombre
        # crudo de carpeta/archivo, y nombres de temporada (ver
        # _matches_query) — ignorando espacios en ambos lados.
        query = _normalize_search(self.search_input.text().strip())
        cross_library = bool(query)

        if self._viewing_continue_watching and not cross_library:
            self._render_continue_watching_grid()
            return

        if cross_library:
            pool = [
                (name, subpath, poster_bytes, raw_name, sort_key, lib_id)
                for lib_id, entries in self._items_cache.items()
                for (name, subpath, poster_bytes, raw_name, sort_key) in entries
            ]
            items = [
                entry for entry in pool
                if self._matches_query(query, entry[0], entry[3], entry[5], entry[1])
            ]
        else:
            current_lib_id = self._current_library.id if self._current_library else None
            pool = [
                (name, subpath, poster_bytes, raw_name, sort_key, current_lib_id)
                for (name, subpath, poster_bytes, raw_name, sort_key) in self._current_items
            ]
            items = pool

        # entry[4] (sort_key), no entry[0] (nombre mostrado): así una
        # película con nombre de ordenación custom (ver
        # library_metadata.set_movie_sort_key) respeta ese orden en vez
        # del alfabético por título — sort_key cae al nombre mostrado si
        # nunca se seteó uno custom (ScanWorker.run()), así que series y
        # películas sin corregir ordenan exactamente igual que antes.
        items = sorted(items, key=lambda entry: entry[4].lower(), reverse=not self._sort_ascending)

        multi_library = len(self._items_cache) > 1
        total = len(pool)
        if cross_library:
            scope = " en todas las bibliotecas" if multi_library else ""
            self.status_label.setText(f"{len(items)} de {total} ítems encontrados{scope}.")
        else:
            self.status_label.setText(f"{total} ítems encontrados.")

        self._clear_grid()
        for i, (name, subpath, poster_bytes, raw_name, _sort_key, lib_id) in enumerate(items):
            library = self._library_by_id.get(lib_id)
            pixmap = QPixmap()
            if poster_bytes:
                pixmap.loadFromData(poster_bytes)
            badge = library.name if (cross_library and multi_library and library) else None
            tile = PosterTile(
                name, pixmap, badge_text=badge,
                on_click=lambda lib=library, sp=subpath, n=name: self._on_item_clicked(lib, sp, n),
            )
            row, col = divmod(i, GRID_COLUMNS)
            self.grid_layout.addWidget(tile, row, col)

    def _on_scan_failed(self, message):
        self.status_label.setText(message)

    def _on_item_clicked(self, library, subpath, name):
        # Cualquier click con el buscador activo (misma biblioteca o
        # distinta) tiene que limpiarlo y dejar la pestaña bien asentada
        # — antes solo pasaba si la serie pertenecía a OTRA biblioteca; si
        # ya estaba en la biblioteca activa (buscando "kiddy grade" con
        # "Anime WDC" ya seleccionada, ej.), el buscador quedaba con texto
        # y "Volver" mostraba el resultado de búsqueda en vez de la
        # biblioteca completa (bug real reportado en vivo).
        if library and self.search_input.text():
            self._switch_active_library(library)
        if library.type == "movies":
            self.play_requested.emit(library, subpath, name)
        else:
            self.series_requested.emit(library, subpath, name)

    def _clear_grid(self):
        while self.grid_layout.count():
            item = self.grid_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

    def refresh_continue_watching(self):
        """Público — llamado por MainWindow al volver a la galería (desde
        la lista de episodios o desde el reproductor), porque el progreso
        pudo haber cambiado durante esa sesión de reproducción."""
        # Además del progreso de ESTA sesión, relee el de las bibliotecas
        # centralizadas por si se vio algo desde otra máquina — en
        # background, así que la galería se pinta ya con lo que hay en
        # cache y se vuelve a pintar sola cuando llega lo nuevo.
        self._start_progress_refresh()
        self._update_continue_watching_tab_visibility()
        if not self._viewing_continue_watching:
            return
        if not self._continue_watching_button.isVisible():
            # Se estaba viendo "Continuar viendo" y el último ítem en
            # progreso se terminó de ver justo ahora (ej. se dejó
            # correr hasta el final) — la tab activa desapareció, hay
            # que caer a alguna biblioteca real en vez de dejar la
            # grilla mostrando datos huérfanos sin ninguna tab marcada.
            libraries = list(self._library_by_id.values())
            if libraries:
                self._tab_buttons[libraries[0].id].setChecked(True)
                self._on_library_selected(libraries[0])
            return
        self._render_grid()

    def _start_progress_refresh(self):
        if self._progress_refresh_worker is not None and self._progress_refresh_worker.isRunning():
            return  # ya hay uno en vuelo (ej. se entró y salió rápido de una serie)
        libraries = list(self._library_by_id.values())
        if not any(getattr(lib, "progress_centralized", False) for lib in libraries):
            return  # nada centralizado — no hay nada que releer de la red
        self._progress_refresh_worker = ProgressRefreshWorker(libraries)
        self._progress_refresh_worker.refreshed.connect(self._on_progress_refreshed)
        self._progress_refresh_worker.start()

    def _on_progress_refreshed(self):
        # Llega ya en el hilo de Qt (signal cross-thread). Puede haber
        # aparecido progreso nuevo de otra máquina, o haber desaparecido
        # (allá se terminó de ver) — las dos cosas cambian la visibilidad
        # de la tab, no solo el contenido de la grilla.
        self._update_continue_watching_tab_visibility()
        if self._viewing_continue_watching and self._continue_watching_button.isVisible():
            self._render_grid()

    def _has_resolvable_progress(self):
        return has_resolvable_progress(self._library_by_id)

    def _update_continue_watching_tab_visibility(self):
        # Oculta la tab entera (no solo la deja vacía) cuando no hay nada
        # en progreso — pedido explícito del usuario. Se recalcula acá
        # (no solo en reload_libraries) para que aparezca sola apenas hay
        # algo que mostrar, sin necesitar reabrir la app. Devuelve si hay
        # progreso, para que el caller no tenga que preguntarlo de nuevo
        # (ni caer en isVisible(), que no sirve si la ventana todavía no
        # se mostró — ver _reconcile_continue_watching_after_load).
        has_progress = self._has_resolvable_progress()
        self._continue_watching_button.setVisible(has_progress)
        return has_progress

    def _on_continue_watching_tab_selected(self):
        self._current_library = None
        self._viewing_continue_watching = True
        self.search_input.blockSignals(True)
        self.search_input.clear()
        self.search_input.blockSignals(False)
        self._render_grid()

    def _resolve_continue_watching_entries(self):
        return resolve_continue_watching_entries(self._library_by_id, self._items_cache)

    def _render_continue_watching_grid(self):
        entries = self._resolve_continue_watching_entries()
        self.status_label.setText(
            f"{len(entries)} en progreso." if entries
            else "Nada en progreso todavía — lo que empieces a mirar va a aparecer acá."
        )
        self._clear_grid()
        for i, entry in enumerate(entries):
            pixmap = QPixmap()
            if entry["poster_bytes"]:
                pixmap.loadFromData(entry["poster_bytes"])
            tile = PosterTile(
                entry["name"], pixmap, progress_fraction=entry["fraction"],
                on_click=lambda e=entry: self._on_continue_watching_clicked(e),
            )
            row, col = divmod(i, GRID_COLUMNS)
            self.grid_layout.addWidget(tile, row, col)

    def _on_continue_watching_clicked(self, entry):
        library = entry["library"]
        if entry["is_movie"]:
            self.play_requested.emit(library, entry["episode_subpath"], entry["name"])
        else:
            # Salto directo al episodio guardado (no a la lista) — pedido
            # explícito: "Continuar viendo" es un link directo a donde te
            # quedaste, no un atajo a la serie. MainWindow resuelve esto
            # cargando la lista de episodios en background y jugando el
            # episodio apenas está listo (ver _resume_series_episode).
            self.resume_episode_requested.emit(library, entry["series_subpath"], entry["episode_subpath"], entry["name"])
