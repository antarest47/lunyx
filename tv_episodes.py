"""
Lista de episodios del modo TV — Paso 3 de la iniciativa "modo TV" (ver
CONTEXT.md). Clase completamente separada de episodes.EpisodeListView, a
propósito (mismo criterio que tv_gallery.py con gallery.py). Lo que SÍ se
reusa tal cual: episodes.ScanEpisodesWorker (agnóstico de vista, ya
resuelve nombres custom + carátulas de temporada + sondeo de pistas de
audio/sub reales) y los helpers puros episodes._build_track_options/
episodes.AUTO_OPTION.

Alcance deliberadamente MENOR al de escritorio (decidido con el usuario
antes de implementar): sin renombrar serie/temporada (necesitaría un
teclado en pantalla, no tiene sentido en TV) y sin "marcar serie como
vista". SÍ incluye elegir audio/subtítulos por defecto para la serie —
pero no con un QComboBox real: su popup no es navegable con el filtro
espacial global de tv_input (ver TVValuePicker y
tv_input.TV_OWNS_HORIZONTAL_NAV_ATTR).
"""

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

import episodes
import progress_store
import theme
import track_prefs
import tv_input

TV_SERIES_POSTER_W, TV_SERIES_POSTER_H = 220, 320
TV_SEASON_POSTER_W, TV_SEASON_POSTER_H = 160, 230
TV_CONTENT_MAX_WIDTH = 1100  # mismo criterio que episodes.CONTENT_MAX_WIDTH, escalado a la carátula más grande de TV


class TVValuePicker(QWidget):
    """Fila "Etiqueta: ‹ valor ›", navegable con Izquierda/Derecha para
    ciclar entre `options` ([(label, value), ...]) — reemplaza al
    QComboBox de escritorio (episodes.py) porque su popup desplegable no
    es compatible con la navegación espacial global de tv_input: el
    filtro está instalado en QApplication e intercepta las flechas ANTES
    de que puedan llegar al popup. Se marca con
    tv_input.TV_OWNS_HORIZONTAL_NAV_ATTR para que ese filtro le deje pasar
    Izquierda/Derecha (Arriba/Abajo siguen navegando normal, así el picker
    sigue siendo alcanzable como cualquier otro widget)."""

    value_changed = Signal(int)  # índice elegido en `options`
    activated = Signal()  # Enter/Return/Select — ver tv_player.py, "usar para toda la serie"

    def __init__(self, label_text):
        super().__init__()
        setattr(self, tv_input.TV_OWNS_HORIZONTAL_NAV_ATTR, True)
        self._options = [("", None)]
        self._index = 0
        self.setObjectName("tvValuePicker")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        label = QLabel(label_text)
        label.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 15px; background: transparent;")

        self._value_label = QLabel("")
        self._value_label.setStyleSheet(
            f"color: {theme.TEXT}; font-size: 15px; font-weight: 600; background: transparent;"
        )

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 10, 16, 10)
        layout.setSpacing(14)
        layout.addWidget(label)
        layout.addStretch()
        layout.addWidget(self._value_label)

    def set_options(self, options, current_index=0):
        self._options = options or [("", None)]
        self._index = max(0, min(current_index, len(self._options) - 1))
        self._refresh_value_label()

    def current(self):
        """(label, value) actualmente seleccionado."""
        return self._options[self._index]

    def _refresh_value_label(self):
        label, _value = self._options[self._index]
        if len(self._options) > 1:
            self._value_label.setText(f"‹ {label} ›")
        else:
            self._value_label.setText(label)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Left:
            self._cycle(-1)
            return
        if event.key() == Qt.Key.Key_Right:
            self._cycle(1)
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Select):
            self.activated.emit()
            return
        super().keyPressEvent(event)

    def _cycle(self, delta):
        if len(self._options) <= 1:
            return
        self._index = (self._index + delta) % len(self._options)
        self._refresh_value_label()
        self.value_changed.emit(self._index)


class TVEpisodeRow(QWidget):
    """Equivalente TV de episodes.EpisodeRow — focusable (StrongFocus) en
    vez de clickeable por mouse, Enter/Return/Select disparan `clicked`
    igual que TVPosterTile. Misma barra de progreso fina pegada abajo."""

    clicked = Signal(tuple, str)
    PROGRESS_BAR_HEIGHT = 4

    def __init__(self, index_label, subpath, title, progress_fraction=None):
        super().__init__()
        self._subpath = subpath
        self._title = title
        self._progress_fraction = progress_fraction
        self.setObjectName("tvEpisodeRow")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        number_label = QLabel(index_label)
        number_label.setFixedWidth(40)
        number_label.setStyleSheet(f"color: {theme.ACCENT}; font-weight: 700; font-size: 16px; background: transparent;")

        title_label = QLabel(title)
        title_label.setStyleSheet("color: #d7d9de; font-size: 16px; background: transparent;")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(14)
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

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Select):
            self.clicked.emit(self._subpath, self._title)
            return
        super().keyPressEvent(event)


class TVEpisodeListView(QWidget):
    """Emite episode_selected(subpath, title) al elegir un episodio,
    series_renamed(library, series_subpath, name) cuando el scan resuelve
    un nombre custom (TV no renombra, pero SÍ tiene que mostrar/propagar
    el nombre resuelto — ver gallery.patch_series_name) y
    episodes_ready() cuando terminó de escanear y flat_episodes ya está
    poblado (mismo contrato que episodes.EpisodeListView, para que un
    futuro Paso 4 pueda reusar el mismo flujo de "saltar directo a un
    episodio" que ya usa el modo desktop)."""

    episode_selected = Signal(tuple, str)
    series_renamed = Signal(object, tuple, str)
    episodes_ready = Signal()

    def __init__(self, on_back):
        super().__init__()
        self._scan_worker = None
        self._library = None
        self._series_subpath = None
        self._series_key = None
        self.flat_episodes = []  # [(subpath, title, group_key, kind), ...] mismo formato que episodes.py
        self._row_by_subpath = {}  # subpath -> TVEpisodeRow, ver reload(focus_subpath=...)
        self._pending_focus_subpath = None

        self._audio_presets = [episodes.AUTO_OPTION]
        self._subtitle_presets = [episodes.AUTO_OPTION]

        self._on_back = on_back

        self.back_button = QPushButton("‹ Volver a la galería")
        self.back_button.setObjectName("tvBackButton")
        self.back_button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.back_button.setAutoDefault(True)  # ver nota en tv_gallery.manage_button
        self.back_button.clicked.connect(on_back)

        top_bar_row = QHBoxLayout()
        top_bar_row.setContentsMargins(10, 8, 10, 8)
        top_bar_row.addWidget(self.back_button)
        top_bar_row.addStretch()

        self.header_bar = QWidget()
        self.header_bar.setObjectName("tvHeaderBar")
        self.header_bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.header_bar.setLayout(top_bar_row)

        self.series_poster_label = QLabel()
        self.series_poster_label.setFixedSize(TV_SERIES_POSTER_W, TV_SERIES_POSTER_H)
        self.series_poster_label.setStyleSheet("background-color: #23262e; border-radius: 10px;")
        self.series_poster_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        poster_shadow = QGraphicsDropShadowEffect(self)
        poster_shadow.setBlurRadius(28)
        poster_shadow.setOffset(0, 4)
        poster_shadow.setColor(Qt.GlobalColor.black)
        self.series_poster_label.setGraphicsEffect(poster_shadow)

        self.series_title_label = QLabel("")
        self.series_title_label.setStyleSheet("font-size: 28px; font-weight: 700; color: #f2f3f5;")

        self.audio_picker = TVValuePicker("Audio por defecto")
        self.subtitle_picker = TVValuePicker("Subtítulos por defecto")
        self.audio_picker.value_changed.connect(self._on_audio_pref_changed)
        self.subtitle_picker.value_changed.connect(self._on_subtitle_pref_changed)

        series_info_col = QVBoxLayout()
        series_info_col.setSpacing(12)
        series_info_col.addWidget(self.series_title_label)
        series_info_col.addWidget(self.audio_picker)
        series_info_col.addWidget(self.subtitle_picker)
        series_info_col.addStretch()

        header_row = QHBoxLayout()
        header_row.setSpacing(24)
        header_row.addWidget(self.series_poster_label)
        header_row.addLayout(series_info_col, stretch=1)

        self.status_label = QLabel("")
        self.status_label.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 14px;")

        # Todo (header + temporadas) en una sola columna acotada,
        # centrada, adentro del único scroll de la página — mismo
        # criterio que episodes.EpisodeListView (sin scrolls anidados por
        # temporada).
        content = QWidget()
        content.setMaximumWidth(TV_CONTENT_MAX_WIDTH)
        self.content_layout = QVBoxLayout(content)
        self.content_layout.setSpacing(20)
        self.content_layout.addLayout(header_row)
        self.content_layout.addWidget(self.status_label)
        self.content_layout.addStretch()  # los grupos se insertan antes de este stretch

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.scroll_area.setWidget(content)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 24, 32, 24)
        layout.setSpacing(14)
        layout.addWidget(self.header_bar)
        layout.addWidget(self.scroll_area)

        # Estilo propio, objectNames "tv*" (nunca tocar theme.py acá, ver
        # principio de "TV no comparte widgets ni estilos con desktop" —
        # mismo criterio que tv_gallery.py).
        self.setStyleSheet(f"""
            QWidget#tvHeaderBar {{
                background-color: {theme.SURFACE};
                border-radius: 10px;
            }}
            QPushButton#tvBackButton {{
                background-color: {theme.BG};
                border: 2px solid transparent;
                border-radius: 8px;
                padding: 10px 18px;
                color: {theme.TEXT_DIM};
                font-weight: 600;
                font-size: 16px;
            }}
            QPushButton#tvBackButton:focus {{
                border: 2px solid {theme.ACCENT};
                color: {theme.ACCENT};
            }}
            QWidget#tvValuePicker {{
                background-color: {theme.SURFACE};
                border-radius: 8px;
                border: 2px solid transparent;
            }}
            QWidget#tvValuePicker:focus {{
                border: 2px solid {theme.ACCENT};
            }}
            QWidget#tvSeasonCard {{
                background-color: {theme.SURFACE};
                border-radius: 10px;
            }}
            QWidget#tvEpisodeRow {{
                background-color: transparent;
                border-radius: 6px;
                border: 2px solid transparent;
            }}
            QWidget#tvEpisodeRow:focus {{
                background-color: rgba({theme.ACCENT_RGB}, 0.15);
                border: 2px solid {theme.ACCENT};
            }}
        """)

        tv_input.install_spatial_navigation(self)
        # Escape = "volver a la galería", pedido explícito del usuario —
        # mismo callback que el botón "Volver".
        tv_input.install_escape_back(self, self._on_back)

        # Mismo fix que TVGalleryView (Paso 28, Ronda 2): sin esto, navegar
        # con flechas movía el foco pero el scroll no lo seguía — acá TODO
        # (header, pickers, filas de episodio) vive adentro de un único
        # scroll_area, así que alcanza con chequear self.isAncestorOf en
        # vez de un contenedor puntual como grid_container en la galería.
        QApplication.instance().focusChanged.connect(self._on_focus_changed_scroll)

    def _on_focus_changed_scroll(self, old, now):
        if now is None:
            return
        if now is self.back_button:
            # back_button vive en header_bar, AFUERA de scroll_area (así
            # queda siempre visible arriba, ver __init__) — el
            # ensureWidgetVisible() de más abajo nunca lo alcanza porque
            # no es descendiente del scroll area. Al navegar hasta acá
            # (ej. Arriba desde el picker de Audio) el scroll de la
            # página se quedaba donde estaba, dejando el título de la
            # serie — arriba de todo el contenido scrolleable — tapado
            # (bug real reportado en vivo). Volver siempre al tope acá
            # deja el título visible de nuevo.
            self.scroll_area.verticalScrollBar().setValue(0)
            return
        if self.scroll_area.isAncestorOf(now):
            self.scroll_area.ensureWidgetVisible(now, 20, 20)

    def showEvent(self, event):
        # Esta vista se reusa para CUALQUIER serie (load() se llama de
        # nuevo cada vez, reconstruyendo las filas/tarjetas — las viejas
        # quedan deleteLater()), así que confiar en que Qt "recuerde" el
        # último foco sería apuntar a un widget ya destruido. En vez de
        # eso, cada vez que la vista se muestra (stack.setCurrentWidget)
        # el foco vuelve a "Volver" — lo único que existe siempre, incluso
        # antes de que termine el scan. Mismo patrón de reactivar la
        # ventana + QTimer.singleShot(0, ...) que
        # TVGalleryView._reactivate_and_focus (Paso 28): setFocus() antes
        # de que la ventana esté realmente activa no "pega".
        super().showEvent(event)
        QTimer.singleShot(0, self._focus_back_button)

    def _focus_back_button(self):
        window = self.window()
        window.raise_()
        window.activateWindow()
        self.back_button.setFocus()

    # ---- carga de la serie (mismo criterio que episodes.EpisodeListView) ----

    def reload(self, focus_subpath=None):
        """focus_subpath: si viene, el episodio que hay que dejar
        resaltado apenas termine el rescan (ver TVMainWindow._player_back
        — Escape desde el reproductor vuelve acá con el episodio que se
        estaba reproduciendo, no con el foco por defecto en 'Volver')."""
        if self._library and self._series_subpath:
            self.load(
                self._library, self._series_subpath, self.series_title_label.text(),
                focus_subpath=focus_subpath,
            )

    def load(self, library, series_subpath, series_name_fallback, focus_subpath=None):
        self._library = library
        self._series_subpath = series_subpath
        self._series_key = series_subpath[0]
        self._pending_focus_subpath = focus_subpath

        self.series_title_label.setText(series_name_fallback)
        self.series_poster_label.clear()
        self.series_poster_label.setText("")
        self._clear_groups()
        self._audio_presets = [episodes.AUTO_OPTION]
        self._subtitle_presets = [episodes.AUTO_OPTION]
        self.audio_picker.set_options(self._audio_presets)
        self.subtitle_picker.set_options(self._subtitle_presets)
        self.status_label.setText(f"Escaneando episodios de '{series_name_fallback}'...")

        self._scan_worker = episodes.ScanEpisodesWorker(library, series_subpath)
        self._scan_worker.finished_ok.connect(self._on_scan_ok)
        self._scan_worker.failed.connect(self._on_scan_failed)
        self._scan_worker.start()

    def _rebuild_track_pref_pickers(self, audio_descriptions, subtitle_descriptions):
        self._audio_presets = episodes._build_track_options(audio_descriptions)
        self._subtitle_presets = episodes._build_track_options(subtitle_descriptions)
        self._load_track_prefs()

    def _load_track_prefs(self):
        prefs = track_prefs.get_prefs(self._library, self._series_key)
        self.audio_picker.set_options(
            self._audio_presets, self._index_for_pref(self._audio_presets, prefs.get("audio"))
        )
        self.subtitle_picker.set_options(
            self._subtitle_presets, self._index_for_pref(self._subtitle_presets, prefs.get("subtitle"))
        )

    def _index_for_pref(self, presets, stored_value):
        for i, (_label, value) in enumerate(presets):
            if value and value == stored_value:
                return i
        return 0

    def _on_audio_pref_changed(self, index):
        _label, value = self._audio_presets[index]
        track_prefs.set_audio_pref(self._library, self._series_key, value)

    def _on_subtitle_pref_changed(self, index):
        _label, value = self._subtitle_presets[index]
        track_prefs.set_subtitle_pref(self._library, self._series_key, value)

    def _on_scan_ok(self, payload):
        self.series_title_label.setText(payload["series_display_name"])
        self.series_renamed.emit(self._library, self._series_subpath, payload["series_display_name"])
        self._rebuild_track_pref_pickers(payload.get("audio_track_options"), payload.get("subtitle_track_options"))
        if payload["series_poster_bytes"]:
            pixmap = QPixmap()
            pixmap.loadFromData(payload["series_poster_bytes"])
            scaled = pixmap.scaled(
                TV_SERIES_POSTER_W, TV_SERIES_POSTER_H,
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            self.series_poster_label.setPixmap(scaled)
        else:
            self.series_poster_label.setText("Sin\nposter")

        self._clear_groups()
        self.flat_episodes = []
        self._row_by_subpath = {}
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

        any_poster = any(group["poster_bytes"] for group in payload["groups"])
        for group in payload["groups"]:
            self._add_group_card(group, show_poster=any_poster)

        self._focus_pending_row()
        self.episodes_ready.emit()

    def _focus_pending_row(self):
        subpath = self._pending_focus_subpath
        self._pending_focus_subpath = None
        if subpath is None:
            return
        row = self._row_by_subpath.get(subpath)
        if not row:
            return
        # Diferido DOS veces (no una): la fila se acaba de crear en esta
        # misma pasada (_add_group_card, más arriba), dentro de un árbol
        # de layouts bastante anidado (fila -> columna -> tarjeta ->
        # content_layout -> scroll_area) — un solo QTimer.singleShot(0,...)
        # (mismo patrón que TVGalleryView._focus_default_tab) no le
        # alcanzaba a Qt para terminar de propagar la geometría final por
        # TODOS esos niveles antes de que ensureWidgetVisible() calculara
        # el scroll, dando un resultado incorrecto en la app real (bug
        # reportado en vivo, confirmado con un test que veía la barra de
        # scroll moverse pero a un valor que no traía la fila a la
        # vista). Encadenar un segundo singleShot(0,...) le da a Qt una
        # vuelta extra del event loop para terminar de asentar todo.
        QTimer.singleShot(0, lambda: QTimer.singleShot(0, lambda: self._reveal_row(row)))

    def _reveal_row(self, row):
        row.setFocus()
        self.scroll_area.ensureWidgetVisible(row, 20, 20)

    def _build_subsection_label(self, text):
        label = QLabel(text.upper())
        label.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 12px; font-weight: 600; background: transparent;")
        return label

    def _build_episode_rows(self, items, numbered=True):
        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(4)
        for i, (subpath, title) in enumerate(items, start=1):
            index_label = f"{i:02d}" if numbered else "♪"
            progress = progress_store.get_progress_fraction(self._library, subpath)
            row = TVEpisodeRow(index_label, subpath, title, progress_fraction=progress)
            row.clicked.connect(self._on_episode_row_clicked)
            self._row_by_subpath[subpath] = row
            col.addWidget(row)
        return col

    def _add_group_card(self, group, show_poster=True):
        card = QWidget()
        card.setObjectName("tvSeasonCard")
        card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        poster_label = None
        if show_poster:
            poster_label = QLabel()
            poster_label.setFixedSize(TV_SEASON_POSTER_W, TV_SEASON_POSTER_H)
            poster_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            if group["poster_bytes"]:
                pixmap = QPixmap()
                pixmap.loadFromData(group["poster_bytes"])
                scaled = pixmap.scaled(
                    TV_SEASON_POSTER_W, TV_SEASON_POSTER_H,
                    Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation,
                )
                poster_label.setPixmap(scaled)
                poster_label.setStyleSheet("border-radius: 8px; background: transparent;")
            else:
                poster_label.setText("Sin\nposter")
                poster_label.setStyleSheet(
                    "background-color: #2a2d35; border-radius: 8px; color: #6b7078;"
                )

        title_label = QLabel(group["label"])
        title_label.setStyleSheet(
            f"font-size: 18px; font-weight: 600; color: {theme.ACCENT}; background: transparent;"
        )

        right_col = QVBoxLayout()
        right_col.setSpacing(12)
        right_col.addWidget(title_label)

        if group["episodes"]:
            right_col.addLayout(self._build_episode_rows(group["episodes"], numbered=True))
        if group["extras"]:
            right_col.addWidget(self._build_subsection_label("Especiales"))
            right_col.addLayout(self._build_episode_rows(group["extras"], numbered=True))
        if group["openings_endings"]:
            right_col.addWidget(self._build_subsection_label("Openings / Endings"))
            right_col.addLayout(self._build_episode_rows(group["openings_endings"], numbered=False))

        card_layout = QHBoxLayout(card)
        card_layout.setContentsMargins(20, 20, 20, 20)
        card_layout.setSpacing(20)
        if poster_label is not None:
            card_layout.addWidget(poster_label, alignment=Qt.AlignmentFlag.AlignTop)
        card_layout.addLayout(right_col, stretch=1)

        self.content_layout.insertWidget(self.content_layout.count() - 1, card)

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
