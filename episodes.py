"""
Vista de lista de episodios de una serie: se abre al clickear una serie en
la galería. El scanning corre en QThread aparte (network I/O sobre SMB, no
debe congelar la ventana).

También resuelve nombres personalizados de serie/temporada
(library_metadata.py, guardado en el server, no localmente) y carátulas
de temporada (archivos "TemN" en la carpeta de la serie, ver
smb_scanner.find_season_poster).
"""

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

import centralized_store
import credentials
import library_metadata
import progress_store
import smb_scanner
import theme
import track_prefs
from widgets import BackButton

SERIES_POSTER_W, SERIES_POSTER_H = 180, 260
SEASON_POSTER_W, SEASON_POSTER_H = 130, 185
CONTENT_MAX_WIDTH = 900  # mismo orden de magnitud que la grilla de la galería (5 posters de 150px + gaps)

# El combo del header copia EXACTAMENTE las mismas opciones que vería el
# usuario abriendo el primer episodio en el reproductor (mismas
# descripciones de pista reales de libVLC, mismo orden, incluyendo
# "Deshabilitar") — sondeadas con libVLC (ver track_probe.py) al agregar
# la biblioteca o al pedir "Actualizar biblioteca" (gallery.
# LibraryTrackProbeWorker), NUNCA acá on-demand (ver
# ScanEpisodesWorker._resolve_track_options para el porqué). El resultado
# queda cacheado server-side (library_metadata.get/set_track_probe); si
# esta serie todavía no pasó por ningún sondeo, solo se ofrece
# "Automático (primera pista)" — la única opción agregada que el
# reproductor no tiene, no guarda ninguna preferencia, deja que VLC elija
# su default.
AUTO_OPTION = ("Automático (primera pista)", "")


def _build_track_options(descriptions):
    """descriptions: lista de strings (descripciones reales de pista,
    tal cual las devuelve track_probe.probe_tracks), o None si esta serie
    todavía no pasó por ningún sondeo — en ese caso solo se ofrece
    "Automático" hasta la próxima vez que se agregue/actualice la
    biblioteca."""
    if descriptions is None:
        return [AUTO_OPTION]
    return [AUTO_OPTION] + [(text, text) for text in descriptions]


def _build_titles(entries, effective_name, has_custom, kind_label, number_if_single=True):
    """Arma (subpath, título) por entrada. Sin nombre custom (de serie o
    temporada) se usa el nombre de archivo tal cual — mismo criterio que
    los episodios normales. 'number_if_single=False' es para
    Opening/Ending: con uno solo alcanza "Opening", no hace falta
    "Opening 1"."""
    items = []
    total = len(entries)
    for i, entry in enumerate(entries, start=1):
        if has_custom:
            if total > 1 or number_if_single:
                title = f"{effective_name} - {kind_label} {i}"
            else:
                title = f"{effective_name} - {kind_label}"
        else:
            title = entry.name
        items.append((entry.subpath, title))
    return items


class ScanEpisodesWorker(QThread):
    """A diferencia del scan de galería, acá también se resuelven nombres
    custom (library_metadata) y carátulas de temporada — todo en el mismo
    thread de background, ya que igual hay que pegarle a SMB por los
    episodios."""

    finished_ok = Signal(dict)
    failed = Signal(str)

    def __init__(self, library, series_subpath):
        super().__init__()
        self.library = library
        self.series_subpath = series_subpath

    def run(self):
        password = credentials.get_password(self.library.id, self.library.username) or ""

        # Abrir una serie RELEE el progreso centralizado (refresh, no solo
        # preload): es el momento natural para enterarse de lo que se vio
        # desde otra máquina (laptop -> escritorio -> HTPC), que es el
        # punto de centralizar. No-op si la biblioteca no centraliza, y
        # respeta escrituras locales todavía sin flushear (ver
        # RemoteJsonStore.invalidate). El uid compartido se resuelve
        # primero, con la contraseña de la BIBLIOTECA (el sidecar vive
        # ahí), no la del target de progreso.
        centralized_store.resolve_library_uid(self.library, password)
        progress_store.refresh(self.library)
        track_prefs.refresh(self.library)

        series_key = self.series_subpath[0]
        try:
            overrides = library_metadata.get_all_episode_overrides(self.library, password, series_key)
            groups = smb_scanner.scan_episodes(self.library, password, self.series_subpath, overrides=overrides)

            series_display_name = library_metadata.get_series_name(
                self.library, password, series_key, fallback=series_key
            )
            has_series_custom = library_metadata.get_raw_series_name(
                self.library, password, series_key
            ) is not None

            audio_track_options, subtitle_track_options = self._resolve_track_options(password, series_key)

            series_poster_subpath = smb_scanner.find_poster(self.library, *self.series_subpath)
            series_poster_bytes = smb_scanner.fetch_poster_bytes(self.library, series_poster_subpath)

            rendered_groups = []
            for group in groups:
                season_display_name = library_metadata.get_season_name(
                    self.library, password, series_key, group.key, fallback=group.label
                )
                has_season_custom = library_metadata.get_raw_season_name(
                    self.library, password, series_key, group.key
                ) is not None

                season_poster_subpath = smb_scanner.find_season_poster(
                    self.library, self.series_subpath, group.season_number
                )
                season_poster_bytes = smb_scanner.fetch_poster_bytes(self.library, season_poster_subpath)

                if has_season_custom:
                    effective_name, has_custom = season_display_name, True
                elif has_series_custom:
                    effective_name, has_custom = series_display_name, True
                else:
                    effective_name, has_custom = None, False

                episode_items = _build_titles(group.episodes, effective_name, has_custom, "Episodio")
                extra_items = _build_titles(group.extras, effective_name, has_custom, "Especial")
                opening_ending_items = (
                    _build_titles(group.openings, effective_name, has_custom, "Opening", number_if_single=False)
                    + _build_titles(group.endings, effective_name, has_custom, "Ending", number_if_single=False)
                )

                rendered_groups.append({
                    "key": group.key,
                    "label": season_display_name,
                    "poster_bytes": season_poster_bytes,
                    "episodes": episode_items,
                    "extras": extra_items,
                    "openings_endings": opening_ending_items,
                })
        except smb_scanner.StorageError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:
            self.failed.emit(f"Error inesperado escaneando episodios: {exc}")
        else:
            self.finished_ok.emit({
                "series_display_name": series_display_name,
                "series_poster_bytes": series_poster_bytes,
                "groups": rendered_groups,
                "audio_track_options": audio_track_options,
                "subtitle_track_options": subtitle_track_options,
            })

    def _resolve_track_options(self, password, series_key):
        """(audio_descriptions, subtitle_descriptions): las descripciones
        de pista REALES ya cacheadas server-side (ver
        library_metadata.set_track_probe), o (None, None) si esta serie
        todavía no pasó por ningún sondeo — episodes.py cae al fallback
        de mostrar solo "Automático" en ese caso.

        Ya NO sondea acá on-demand (sacado a propósito, no es un olvido):
        el sondeo (track_probe.probe_tracks, su propia instancia de
        libVLC) corriendo en este QThread de background podía solaparse
        con una reproducción REAL corriendo al mismo tiempo en el thread
        principal (otra instancia de libVLC) — ambas tocando el mismo
        plugin SMB de libVLC (libsmbclient, que no es thread-safe pese a
        su API "nueva") a la vez. Eso corrompía memoria trackeada por
        talloc y colgaba TODA la app (crash real reproducido en vivo,
        mismo síntoma exacto documentado en
        https://github.com/MusicPlayerDaemon/MPD/issues/991 con otro
        reproductor — bug de terceros en libsmbclient, no arreglable
        desde acá). El sondeo pasa a correr en dos momentos nada más,
        ninguno concurrente con reproducción real: al agregar una
        biblioteca (gallery.LibraryTrackProbeWorker, ya existía) o al
        pedir "Actualizar biblioteca" (mismo worker, agregado ahí
        también) — ver CONTEXT.md, Paso 33."""
        cached = library_metadata.get_track_probe(self.library, password, series_key)
        if cached is None:
            return None, None
        return cached.get("audio"), cached.get("subtitle")


class MetadataSaveWorker(QThread):
    """Guardar un nombre custom pega al server (SMB) — se hace en
    background para no congelar la UI en el click del lápiz."""

    finished_ok = Signal()
    failed = Signal(str)

    def __init__(self, save_fn):
        super().__init__()
        self._save_fn = save_fn

    def run(self):
        try:
            self._save_fn()
        except Exception as exc:
            self.failed.emit(f"No se pudo guardar el nombre: {exc}")
        else:
            self.finished_ok.emit()


def _make_pencil_button():
    button = QPushButton("✎")
    button.setObjectName("flatIconButton")
    button.setFixedWidth(28)
    button.setToolTip("Renombrar")
    return button


class EpisodeRow(QWidget):
    """Una fila de episodio clickeable, sin QListWidget — así no tiene su
    propio scroll independiente, es un widget más dentro de la tarjeta de
    temporada y scrollea junto con el resto de la página.

    'progress_fraction' (0.0-1.0, o None si nunca se reprodujo / ya está
    visto) dibuja una barra fina ámbar pegada abajo de la fila, con el
    ancho proporcional a lo reproducido — mismo lenguaje visual que el
    scrubber del reproductor. Se re-posiciona a mano en resizeEvent
    porque es un QWidget hijo posicionado con setGeometry(), no algo que
    un layout pueda expresar (necesita un ancho = fracción × ancho de la
    fila, no un tamaño fijo ni un stretch)."""

    clicked = Signal(tuple, str)
    PROGRESS_BAR_HEIGHT = 3

    def __init__(self, index_label, subpath, title, progress_fraction=None):
        super().__init__()
        self._subpath = subpath
        self._title = title
        self._progress_fraction = progress_fraction
        self.setObjectName("episodeRow")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        number_label = QLabel(index_label)
        number_label.setFixedWidth(28)
        number_label.setStyleSheet(f"color: {theme.ACCENT}; font-weight: 700; background: transparent;")

        title_label = QLabel(title)
        title_label.setStyleSheet("color: #d7d9de; background: transparent;")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 7, 8, 7)
        layout.setSpacing(10)
        layout.addWidget(number_label)
        layout.addWidget(title_label, stretch=1)

        self._progress_bar = None
        if progress_fraction:
            self._progress_bar = QWidget(self)
            self._progress_bar.setStyleSheet(f"background-color: {theme.ACCENT}; border: none;")
            self._progress_bar.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            self._update_progress_bar_geometry()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_progress_bar_geometry()

    def _update_progress_bar_geometry(self):
        if not self._progress_bar:
            return
        fraction = max(0.0, min(1.0, self._progress_fraction))
        width = round(self.width() * fraction)
        self._progress_bar.setGeometry(0, self.height() - self.PROGRESS_BAR_HEIGHT, width, self.PROGRESS_BAR_HEIGHT)

    def mousePressEvent(self, event):
        self.clicked.emit(self._subpath, self._title)
        super().mousePressEvent(event)


class EpisodeListView(QWidget):
    """Emite episode_selected(subpath, title) cuando el usuario clickea un
    episodio, series_renamed(library, series_subpath, name) cuando un
    rename de serie/temporada termina de guardarse — así la galería puede
    parchear el nombre cacheado sin re-escanear SMB (ver
    GalleryView.update_series_name) — y episodes_ready() cuando un scan
    termina y flat_episodes ya está poblado (usado por
    MainWindow._resume_series_episode para saltar directo a un episodio
    apenas la lista está lista, sin que el usuario vea la lista primero)."""

    episode_selected = Signal(tuple, str)
    series_renamed = Signal(object, tuple, str)
    episodes_ready = Signal()

    def __init__(self, on_back):
        super().__init__()
        self._scan_worker = None
        self._save_worker = None
        self._library = None
        self._series_subpath = None
        self._series_key = None
        self.flat_episodes = []  # [(subpath, name), ...] en el orden mostrado, para next/prev del player

        back_button = BackButton("Volver a la galería")
        back_button.clicked.connect(on_back)

        self.mark_watched_button = QPushButton("Marcar serie como vista")
        self.mark_watched_button.clicked.connect(self._mark_series_watched)

        # Barra de ancho completo (mismo estilo que #tabsBar de la
        # galería — pedido explícito del usuario, "mismo formato con
        # borde redondeado y fondo diferente"), en vez del viejo
        # top_row_container acotado a CONTENT_MAX_WIDTH y centrado a mano:
        # ese ancho variable hacía que el botón "Volver" quedara en una
        # posición horizontal distinta según el ancho de la ventana, y
        # distinta encima de la posición que usa PlayerView (que no tenía
        # ese límite de ancho) — quedaba inconsistente entre las dos
        # vistas. Esta barra, en cambio, siempre ocupa el ancho completo,
        # con el botón de volver pegado a la izquierda y "Marcar como
        # vista" a la derecha, sin importar el ancho de la columna de
        # contenido de abajo. Se deja AFUERA del scroll (no entra en
        # `content`) a propósito, para que siga visible siempre.
        top_bar_row = QHBoxLayout()
        top_bar_row.setContentsMargins(10, 8, 10, 8)
        top_bar_row.addWidget(back_button)
        top_bar_row.addStretch()
        top_bar_row.addWidget(self.mark_watched_button)

        self.header_bar = QWidget()
        self.header_bar.setObjectName("headerBar")
        self.header_bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.header_bar.setLayout(top_bar_row)

        # --- Header: carátula de la serie + nombre + audio/subs por defecto ---
        self.series_poster_label = QLabel()
        self.series_poster_label.setFixedSize(SERIES_POSTER_W, SERIES_POSTER_H)
        self.series_poster_label.setStyleSheet("background-color: #23262e; border-radius: 8px;")
        self.series_poster_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        poster_shadow = QGraphicsDropShadowEffect(self)
        poster_shadow.setBlurRadius(24)
        poster_shadow.setOffset(0, 4)
        poster_shadow.setColor(Qt.GlobalColor.black)
        self.series_poster_label.setGraphicsEffect(poster_shadow)

        self.series_title_label = QLabel("")
        self.series_title_label.setStyleSheet("font-size: 22px; font-weight: 700; color: #f2f3f5;")
        self.series_rename_button = _make_pencil_button()
        self.series_rename_button.clicked.connect(self._rename_series)

        series_title_row = QHBoxLayout()
        series_title_row.setSpacing(10)
        series_title_row.addWidget(self.series_title_label)
        series_title_row.addWidget(self.series_rename_button)
        series_title_row.addStretch()

        # Los items reales se llenan por serie en _rebuild_track_pref_combos
        # (con los idiomas que la serie realmente tiene, ver
        # ScanEpisodesWorker._resolve_track_langs) — acá arrancan vacíos.
        self.audio_pref_combo = QComboBox()
        self.subtitle_pref_combo = QComboBox()
        self._audio_presets = [AUTO_OPTION]
        self._subtitle_presets = [AUTO_OPTION]
        self.audio_pref_combo.currentIndexChanged.connect(self._on_audio_pref_changed)
        self.subtitle_pref_combo.currentIndexChanged.connect(self._on_subtitle_pref_changed)

        audio_pref_row = QHBoxLayout()
        audio_pref_row.setSpacing(8)
        audio_pref_row.addWidget(QLabel("Audio por defecto:"))
        audio_pref_row.addWidget(self.audio_pref_combo)
        audio_pref_row.addStretch()

        subtitle_pref_row = QHBoxLayout()
        subtitle_pref_row.setSpacing(8)
        subtitle_pref_row.addWidget(QLabel("Subtítulos por defecto:"))
        subtitle_pref_row.addWidget(self.subtitle_pref_combo)
        subtitle_pref_row.addStretch()

        pref_hint = QLabel(
            "Se aplica a toda la serie (o a una temporada específica, si tiene\n"
            "su propia pista elegida desde los controles del reproductor)."
        )
        pref_hint.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")

        series_info_col = QVBoxLayout()
        series_info_col.setSpacing(8)
        series_info_col.addLayout(series_title_row)
        series_info_col.addLayout(audio_pref_row)
        series_info_col.addLayout(subtitle_pref_row)
        series_info_col.addWidget(pref_hint)
        series_info_col.addStretch()

        header_row = QHBoxLayout()
        header_row.setSpacing(20)
        header_row.addWidget(self.series_poster_label)
        header_row.addLayout(series_info_col, stretch=1)

        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: #8a8f99; font-size: 12px;")

        # Todo (header + temporadas) va en una sola columna de ancho
        # acotado, centrada, adentro del único scroll de la página — sin
        # scrolls anidados por temporada.
        content = QWidget()
        content.setMaximumWidth(CONTENT_MAX_WIDTH)
        self.content_layout = QVBoxLayout(content)
        self.content_layout.setSpacing(16)
        self.content_layout.addLayout(header_row)
        self.content_layout.addWidget(self.status_label)
        self.content_layout.addStretch()  # los grupos se insertan antes de este stretch

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        scroll.setWidget(content)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)
        layout.addWidget(self.header_bar)
        layout.addWidget(scroll)

    def reload(self):
        """Re-escanea la serie actual — usado al volver del reproductor
        (ver MainWindow._player_back en main.py), así la barra de
        progreso de cada fila (EpisodeRow) refleja lo que se acaba de
        ver, no lo que había cuando se abrió la lista por primera vez."""
        if self._library and self._series_subpath:
            self.load(self._library, self._series_subpath, self.series_title_label.text())

    def load(self, library, series_subpath, series_name_fallback):
        self._library = library
        self._series_subpath = series_subpath
        self._series_key = series_subpath[0]

        self.series_title_label.setText(series_name_fallback)
        self.series_poster_label.clear()
        self.series_poster_label.setText("")
        self._clear_groups()
        # Mientras se escanea (y se sondean pistas reales, si hace falta)
        # los combos quedan reducidos a solo "Automático" — mostrar los
        # de la serie ANTERIOR ahí sería directamente engañoso, el mismo
        # problema que se está arreglando acá.
        self._audio_presets = [AUTO_OPTION]
        self._subtitle_presets = [AUTO_OPTION]
        self._fill_combo(self.audio_pref_combo, self._audio_presets)
        self._fill_combo(self.subtitle_pref_combo, self._subtitle_presets)
        self.status_label.setText(f"Escaneando episodios de '{series_name_fallback}'...")

        self._scan_worker = ScanEpisodesWorker(library, series_subpath)
        self._scan_worker.finished_ok.connect(self._on_scan_ok)
        self._scan_worker.failed.connect(self._on_scan_failed)
        self._scan_worker.start()

    def _rebuild_track_pref_combos(self, audio_descriptions, subtitle_descriptions):
        self._audio_presets = _build_track_options(audio_descriptions)
        self._subtitle_presets = _build_track_options(subtitle_descriptions)
        self._fill_combo(self.audio_pref_combo, self._audio_presets)
        self._fill_combo(self.subtitle_pref_combo, self._subtitle_presets)
        self._load_track_prefs()

    def _fill_combo(self, combo, presets):
        combo.blockSignals(True)
        combo.clear()
        for label, _value in presets:
            combo.addItem(label)
        combo.blockSignals(False)

    def _load_track_prefs(self):
        prefs = track_prefs.get_prefs(self._library, self._series_key)
        self._set_combo_from_pref(self.audio_pref_combo, self._audio_presets, prefs.get("audio"))
        self._set_combo_from_pref(self.subtitle_pref_combo, self._subtitle_presets, prefs.get("subtitle"))

    def _set_combo_from_pref(self, combo, presets, stored_value):
        combo.blockSignals(True)
        index = 0
        for i, (_label, value) in enumerate(presets):
            if value and value == stored_value:
                index = i
                break
        combo.setCurrentIndex(index)
        combo.blockSignals(False)

    def _on_audio_pref_changed(self, index):
        _label, value = self._audio_presets[index]
        track_prefs.set_audio_pref(self._library, self._series_key, value)

    def _on_subtitle_pref_changed(self, index):
        _label, value = self._subtitle_presets[index]
        track_prefs.set_subtitle_pref(self._library, self._series_key, value)

    def _on_scan_ok(self, payload):
        self.series_title_label.setText(payload["series_display_name"])
        # Le avisa a la galería el nombre resuelto real, así puede
        # actualizar su caché en memoria sin re-escanear SMB (ver
        # GalleryView.update_series_name). Se emite en cada load(), no
        # solo tras un rename — inofensivo, la galería solo re-renderiza
        # si el nombre efectivamente cambió.
        self.series_renamed.emit(self._library, self._series_subpath, payload["series_display_name"])
        self._rebuild_track_pref_combos(payload.get("audio_track_options"), payload.get("subtitle_track_options"))
        if payload["series_poster_bytes"]:
            pixmap = QPixmap()
            pixmap.loadFromData(payload["series_poster_bytes"])
            scaled = pixmap.scaled(
                SERIES_POSTER_W, SERIES_POSTER_H,
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            self.series_poster_label.setPixmap(scaled)
        else:
            self.series_poster_label.setText("Sin\nposter")

        self._clear_groups()
        # (subpath, title, group_key, kind) — group_key y kind los usa el
        # reproductor para decidir el auto-avance al terminar un episodio
        # (ver PlayerView._on_media_end_qt en main.py): solo avanza solo
        # dentro del mismo group_key (misma temporada/bucket de
        # especiales) y solo si el siguiente ítem es kind == "episode"
        # (nunca salta automático a un opening/ending/extra, ni cruza a la
        # temporada siguiente).
        self.flat_episodes = []
        for group in payload["groups"]:
            key = group["key"]
            self.flat_episodes.extend((sp, title, key, "episode") for sp, title in group["episodes"])
            self.flat_episodes.extend((sp, title, key, "extra") for sp, title in group["extras"])
            self.flat_episodes.extend((sp, title, key, "opening_ending") for sp, title in group["openings_endings"])
        total = len(self.flat_episodes)
        if total == 0:
            self.status_label.setText("No se encontraron episodios (videos) en esta serie.")
            self.episodes_ready.emit()
            return
        self.status_label.setText(f"{total} episodios encontrados.")

        # Si NINGUNA tarjeta de la serie tiene una carátula de temporada
        # real (TemN), mostrar el placeholder "Sin poster" en todas es
        # puro ruido — ya está la carátula grande de la serie arriba. Si
        # AL MENOS UNA sí tiene, se muestra la columna de carátula en
        # TODAS (las que no tienen su propia imagen quedan con el
        # placeholder), para que queden alineadas parejas entre sí.
        any_poster = any(group["poster_bytes"] for group in payload["groups"])
        for group in payload["groups"]:
            self._add_group_card(group, show_poster=any_poster)

        self.episodes_ready.emit()

    def _build_subsection_label(self, text):
        label = QLabel(text.upper())
        label.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px; font-weight: 600; background: transparent;")
        return label

    def _build_episode_list(self, items, numbered=True):
        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)
        for i, (subpath, title) in enumerate(items, start=1):
            index_label = f"{i:02d}" if numbered else "♪"
            progress = progress_store.get_progress_fraction(self._library, subpath)
            row = EpisodeRow(index_label, subpath, title, progress_fraction=progress)
            row.clicked.connect(self._on_episode_row_clicked)
            col.addWidget(row)
        return col

    def _add_group_card(self, group, show_poster=True):
        card = QWidget()
        card.setObjectName("seasonCard")
        card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        poster_label = None
        if show_poster:
            poster_label = QLabel()
            poster_label.setFixedSize(SEASON_POSTER_W, SEASON_POSTER_H)
            poster_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            if group["poster_bytes"]:
                pixmap = QPixmap()
                pixmap.loadFromData(group["poster_bytes"])
                scaled = pixmap.scaled(
                    SEASON_POSTER_W, SEASON_POSTER_H,
                    Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation,
                )
                poster_label.setPixmap(scaled)
                poster_label.setStyleSheet("border-radius: 6px; background: transparent;")
            else:
                poster_label.setText("Sin\nposter")
                poster_label.setStyleSheet(
                    "background-color: #2a2d35; border-radius: 6px; color: #6b7078;"
                )

        title_label = QLabel(group["label"])
        title_label.setStyleSheet(
            f"font-size: 14px; font-weight: 600; color: {theme.ACCENT}; background: transparent;"
        )
        rename_button = _make_pencil_button()
        rename_button.clicked.connect(lambda checked, g=group: self._rename_season(g))

        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(8)
        title_row.addWidget(title_label)
        title_row.addWidget(rename_button)
        title_row.addStretch()

        right_col = QVBoxLayout()
        right_col.setSpacing(10)
        right_col.addLayout(title_row)

        if group["episodes"]:
            right_col.addLayout(self._build_episode_list(group["episodes"], numbered=True))
        if group["extras"]:
            right_col.addWidget(self._build_subsection_label("Especiales"))
            right_col.addLayout(self._build_episode_list(group["extras"], numbered=True))
        if group["openings_endings"]:
            right_col.addWidget(self._build_subsection_label("Openings / Endings"))
            right_col.addLayout(self._build_episode_list(group["openings_endings"], numbered=False))

        card_layout = QHBoxLayout(card)
        card_layout.setContentsMargins(16, 16, 16, 16)
        card_layout.setSpacing(16)
        if poster_label is not None:
            card_layout.addWidget(poster_label, alignment=Qt.AlignmentFlag.AlignTop)
        card_layout.addLayout(right_col, stretch=1)

        self.content_layout.insertWidget(self.content_layout.count() - 1, card)

    def _mark_series_watched(self):
        if not self._library:
            return
        confirm = QMessageBox.question(
            self, "Marcar serie como vista",
            "¿Marcar toda la serie como vista? Esto borra el progreso guardado "
            "de todos sus episodios (no se puede deshacer) y hace que "
            "desaparezca de \"Continuar viendo\".",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        # No hay un flag de "visto" separado en este proyecto — progress_store
        # solo guarda progreso EN CURSO, así que "marcar como vista" equivale
        # a borrar todo el progreso guardado de la serie (ver
        # progress_store.delete_series_entries). reload() refresca las
        # barras de progreso de cada fila para que se vea al toque.
        progress_store.delete_series_entries(self._library, self._series_key)
        self.reload()

    def _rename_series(self):
        if not self._library:
            return
        password = credentials.get_password(self._library.id, self._library.username) or ""
        # Precarga el nombre EFECTIVO (custom si hay, si no el de la carpeta
        # tal cual se ve en pantalla) en vez de solo el custom — así se puede
        # editar/recortar un nombre de carpeta largo en vez de tener que
        # reescribirlo entero desde cero.
        current = self.series_title_label.text()
        text, ok = QInputDialog.getText(
            self, "Renombrar serie",
            "Nombre (vacío para volver al nombre de la carpeta):",
            text=current,
        )
        if not ok:
            return
        text = text.strip()
        self._save_metadata(
            lambda: library_metadata.set_series_name(self._library, password, self._series_key, text)
        )

    def _rename_season(self, group):
        if not self._library:
            return
        password = credentials.get_password(self._library.id, self._library.username) or ""
        # Mismo criterio que _rename_series: precargar el nombre efectivo
        # (group["label"], lo que ya se ve en pantalla), no solo el custom.
        current = group["label"]
        text, ok = QInputDialog.getText(
            self, "Renombrar temporada",
            "Nombre (vacío para volver al nombre por defecto):",
            text=current,
        )
        if not ok:
            return
        text = text.strip()
        season_key = group["key"]
        self._save_metadata(
            lambda: library_metadata.set_season_name(
                self._library, password, self._series_key, season_key, text
            )
        )

    def _save_metadata(self, save_fn):
        self.status_label.setText("Guardando en el servidor...")
        self._save_worker = MetadataSaveWorker(save_fn)
        self._save_worker.finished_ok.connect(self._on_save_ok)
        self._save_worker.failed.connect(self._on_scan_failed)
        self._save_worker.start()

    def _on_save_ok(self):
        # Se re-escanea todo para reflejar el nombre nuevo en el header,
        # los grupos y los títulos de episodio a la vez, en vez de
        # duplicar acá la lógica de "qué nombre le corresponde a cada
        # episodio" que ya vive en ScanEpisodesWorker. series_renamed se
        # emite recién en _on_scan_ok, cuando el nombre resuelto real está
        # disponible (acá todavía es el nombre viejo, load() solo lo pisa
        # con un fallback hasta que el rescan async termine).
        self.load(self._library, self._series_subpath, self.series_title_label.text())

    def _on_episode_row_clicked(self, subpath, title):
        self.episode_selected.emit(subpath, title)

    def _on_scan_failed(self, message):
        self.status_label.setText(message)

    def _clear_groups(self):
        while self.content_layout.count() > 3:  # header_row, status_label, stretch final
            item = self.content_layout.takeAt(2)
            widget = item.widget()
            if widget:
                widget.deleteLater()
