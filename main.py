"""
Lunyx: galería de bibliotecas SMB + reproductor libVLC embebido.

Sin depender de ningún server/API externo: tiene su propio scanner SMB
(smb_scanner.py) y su propia config de bibliotecas (library_store.py).
Las contraseñas SMB se guardan vía keyring/KDE Wallet (credentials.py),
nunca en el JSON de config.
"""

import os
import sys
import time
from pathlib import Path

import vlc
from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QRegion
from PySide6.QtWidgets import (
    QApplication,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QComboBox,
    QInputDialog,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QFrame,
    QLabel,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QStyle,
)

import app_restart
import app_settings
import centralized_store
import credentials
import library_metadata
import player_engine
import player_icons
import progress_store
import smb_scanner
import theme
import track_prefs
import tv_main
from episodes import EpisodeListView, MetadataSaveWorker
from widgets import BackButton
from gallery import GalleryView

# Constantes/format_time del motor de reproducción viven en player_engine.py
# (compartido con el reproductor de TV, ver Paso 4 en CONTEXT.md) —
# re-importadas acá con sus nombres históricos para no tocar cada call site.
from player_engine import (
    DEFAULT_VOLUME,
    MIN_PLAYBACK_SECONDS_FALLBACK,
    MIN_POSITION_FRACTION_FOR_AUTO_ADVANCE,
    PROGRESS_SAVE_INTERVAL_MS,
    RESTART_THRESHOLD_MS,
    SKIP_MS,
    UI_REFRESH_INTERVAL_MS,
    format_time as _format_time,
)

HIDE_CONTROLS_DELAY_MS = 3000
OVERLAY_BAR_HEIGHT = 44
PLAYER_MARGINS = (20, 12, 20, 16)
PLAYER_SPACING = 10
DEFAULT_ASPECT_RATIO = 16 / 9  # hasta que se sepa el tamaño real del video (ver _on_media_playing_qt)
# Opciones del combo "Categoría" del panel de corrección manual (ver
# PlayerView._apply_episode_correction) — el valor es el mismo "kind"
# que ya usa smb_scanner._classify_kind()/route_file(), así que no hace
# falta traducir nada al guardar.
CORRECTION_CATEGORY_OPTIONS = [
    ("Episodio", "episode"),
    ("Especial de la temporada", "extra"),
    ("Especial de toda la serie", "special"),
    ("Opening", "opening"),
    ("Ending", "ending"),
]
CORRECTION_TOGGLE_TEXT_COLLAPSED = "▶ ¿Temporada/episodio/categoría mal detectado?"
CORRECTION_TOGGLE_TEXT_EXPANDED = "▼ ¿Temporada/episodio/categoría mal detectado?"
# Mismo container/toggle de arriba, pero para películas (ver
# PlayerView._refresh_correction_panel) — no tienen temporada/episodio/
# categoría, tienen un nombre de ordenación invisible en su lugar (ver
# library_metadata.set_movie_sort_key).
CORRECTION_TOGGLE_TEXT_MOVIE_COLLAPSED = "▶ Corregir orden en la galería"
CORRECTION_TOGGLE_TEXT_MOVIE_EXPANDED = "▼ Corregir orden en la galería"
MIN_VIDEO_HEIGHT = 220
WIDGET_SIZE_MAX = 16_777_215  # QWIDGETSIZE_MAX (Qt no lo expone directo en PySide6)
VIDEO_TOP_RADIUS = 10  # mismo radio que CONTROL_BAR_STYLE_WINDOWED, para que calcen visualmente

# En modo ventana el control bar queda pegado abajo del video (sin gap),
# como una sola tarjeta — solo las esquinas de ABAJO redondeadas, porque
# las de arriba las "completa" visualmente el video. El video en sí no se
# puede recortar con esquinas redondeadas (libVLC dibuja ahí por fuera del
# sistema de pintado de Qt, ver Paso 1/6 en CONTEXT.md), así que solo la
# barra de abajo puede rendondearse de verdad.
CONTROL_BAR_STYLE_WINDOWED = (
    f"#controlBar {{ background-color: {theme.SURFACE}; "
    "border-bottom-left-radius: 10px; border-bottom-right-radius: 10px; }"
)
# Negro sólido (no semi-transparente): con semi-transparencia se notaban
# los bordes/cajas de cada sub-control (botones, sliders) contra el video
# de fondo. Todo negro parejo hace que esos bordes se vuelvan invisibles.
# Sin bordes redondeados, para que la barra llegue de punta a punta.
CONTROL_BAR_STYLE_FULLSCREEN = "#controlBar { background-color: #000000; border-radius: 0px; }"


class IconButton(QPushButton):
    """Botón con ícono dibujado a mano (QPainter) en vez de un glifo
    Unicode/emoji. Los glifos de distintos símbolos (▶ vs ⏸, por ejemplo)
    tienen métricas de tamaño MUY inconsistentes entre sí en la fuente del
    sistema — el pause se veía visiblemente más chico/fino que el play,
    pedido explícito del usuario de arreglarlo. Dibujar a mano garantiza
    que todos los íconos midan exactamente lo mismo, y de paso permite
    agrandarlos sin depender de si la fuente tiene ese glifo en un tamaño
    grande decente."""

    SIZE = 30

    def __init__(self, icon_name, tooltip=""):
        super().__init__()
        self._icon_name = icon_name
        self.setObjectName("flatIconButton")
        self.setFixedSize(self.SIZE, self.SIZE)
        if tooltip:
            self.setToolTip(tooltip)

    def set_icon_name(self, name):
        if name != self._icon_name:
            self._icon_name = name
            self.update()

    def paintEvent(self, event):
        super().paintEvent(event)  # fondo/hover/pressed normal de #flatIconButton (ver theme.py)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = QColor(theme.BORDER if not self.isEnabled() else theme.TEXT)
        painter.setBrush(color)
        painter.setPen(Qt.PenStyle.NoPen)

        cx, cy = self.width() / 2, self.height() / 2
        s = min(self.width(), self.height()) * 0.16  # unidad base de escala de todos los íconos

        player_icons.paint_icon(painter, self._icon_name, cx, cy, s, color)
        painter.end()


class SeekSlider(QSlider):
    """QSlider de fábrica solo mueve el handle un 'page step' al clickear
    en el track (como una scrollbar) — no salta a la posición del click.
    Se deja que Qt registre el press normalmente primero (así sigue
    andando sliderPressed/el arrastre posterior), y después se pisa el
    valor con el que corresponde al punto exacto donde se clickeó.

    seek_requested se emite directo desde el click en vez de esperar a
    sliderReleased: cuando el click no cae justo sobre el handle (el caso
    normal acá, el handle suele estar lejos de donde se clickea), Qt no
    siempre emite sliderReleased de forma confiable para ese primer click
    — confirmado con un test real donde el valor visual saltaba bien pero
    el seek nunca se aplicaba al reproductor."""

    seek_requested = Signal(int)

    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        if event.button() == Qt.MouseButton.LeftButton:
            value = QStyle.sliderValueFromPosition(
                self.minimum(), self.maximum(), int(event.position().x()), self.width()
            )
            self.setValue(value)
            self.seek_requested.emit(value)


class VideoFrame(QFrame):
    """Lienzo en blanco: le pasamos su window id a libVLC para que dibuje
    el video ahí adentro, por fuera del sistema de dibujado de Qt.

    clicked: click izquierdo sobre el video (play/pause).
    mouse_moved: para reaparecer los controles en fullscreen.
    resized: para reposicionar el overlay de controles en fullscreen
    cuando cambia el tamaño (ej. al entrar/salir de fullscreen)."""

    clicked = Signal()
    mouse_moved = Signal()
    resized = Signal()

    def __init__(self):
        super().__init__()
        self.setMouseTracking(True)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        self.mouse_moved.emit()
        super().mouseMoveEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.resized.emit()


class PlayerView(QWidget):
    back_requested = Signal()
    fullscreen_requested = Signal()
    episode_change_requested = Signal(tuple, str)
    movie_renamed = Signal(object, tuple, str)  # (library, subpath, nuevo nombre) — ver _rename_movie

    def __init__(self):
        super().__init__()
        # Motor compartido con el reproductor de TV (player_engine.py) —
        # instancia de libVLC, set_xwindow, puente de threading de
        # eventos. media_player queda expuesto como atributo propio (en
        # vez de siempre pasar por self._engine.media_player) porque el
        # resto de este archivo ya lo usa así en decenas de lugares.
        self._engine = player_engine.PlayerEngine()
        self.media_player = self._engine.media_player

        self.playlist = []       # [(subpath, title), ...] — vacío si no viene de una serie
        self.playlist_index = -1
        self.series_key = None   # (library_id, series_root) o None si es película

        back_button = BackButton("Volver")
        back_button.clicked.connect(self.back_requested.emit)

        # Barra de ancho completo (mismo estilo #headerBar que usa
        # episodes.py, ver theme.py) en vez del viejo top_row_container
        # metido adentro de content_column — content_column tiene un
        # ancho DINÁMICO (lo calcula _update_player_card_geometry según
        # el aspect ratio real del video), así que el botón "Volver"
        # quedaba en una posición horizontal distinta según qué se
        # estuviera reproduciendo, y encima distinta de la posición fija
        # que usa episodes.py — quedaba inconsistente entre las dos
        # vistas. Esta barra vive afuera de content_column/center_row,
        # directo en self._layout, así siempre ocupa el ancho completo de
        # la ventana sin importar el ancho del video.
        top_row = QHBoxLayout()
        top_row.setContentsMargins(10, 8, 10, 8)
        top_row.addWidget(back_button)
        top_row.addStretch()
        self.header_bar = QWidget()
        self.header_bar.setObjectName("headerBar")
        self.header_bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.header_bar.setLayout(top_row)

        self._video_aspect_ratio = DEFAULT_ASPECT_RATIO  # se actualiza con el tamaño real apenas arranca el video

        self.video_frame = VideoFrame()
        self.video_frame.setStyleSheet("background-color: black;")
        self.video_frame.setMinimumHeight(MIN_VIDEO_HEIGHT)
        # Dentro de player_card, el video se come lo que sobre del alto de
        # la tarjeta (la barra de controles debajo tiene alto fijo por su
        # sizeHint) — el ancho de la tarjeta entera lo fuerza
        # _update_player_card_geometry() según el aspect ratio real.
        self.video_frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        # --- Barra de controles (scrubber real, volumen, fullscreen, next/prev) ---
        self.prev_button = IconButton("prev", "Episodio anterior")
        self.play_pause_button = IconButton("play", "Reproducir/Pausar")
        self.next_button = IconButton("next", "Episodio siguiente")
        self.prev_button.clicked.connect(self._play_previous)
        self.next_button.clicked.connect(self._play_next)
        self.play_pause_button.clicked.connect(self._toggle_play_pause)

        self.elapsed_label = QLabel("0:00")
        self.elapsed_label.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        self.duration_label = QLabel("0:00")
        self.duration_label.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")

        self.seek_slider = SeekSlider(Qt.Orientation.Horizontal)
        self.seek_slider.setRange(0, 0)
        self._seek_dragging = False
        self.seek_slider.sliderPressed.connect(self._on_seek_pressed)
        self.seek_slider.sliderReleased.connect(self._on_seek_released)
        self.seek_slider.seek_requested.connect(self.media_player.set_time)

        self.volume_button = IconButton("volume", "Silenciar")
        self.volume_button.clicked.connect(self._toggle_mute)
        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(DEFAULT_VOLUME)
        self.volume_slider.setFixedWidth(70)
        self.volume_slider.valueChanged.connect(self.media_player.audio_set_volume)

        self.fullscreen_button = IconButton("fullscreen", "Pantalla completa")
        self.fullscreen_button.clicked.connect(self.fullscreen_requested.emit)

        control_bar = QHBoxLayout()
        control_bar.setContentsMargins(8, 4, 8, 4)
        control_bar.setSpacing(6)
        control_bar.addWidget(self.prev_button)
        control_bar.addWidget(self.play_pause_button)
        control_bar.addWidget(self.next_button)
        control_bar.addWidget(self.elapsed_label)
        control_bar.addWidget(self.seek_slider, stretch=1)
        control_bar.addWidget(self.duration_label)
        control_bar.addWidget(self.volume_button)
        control_bar.addWidget(self.volume_slider)
        control_bar.addWidget(self.fullscreen_button)

        self.control_bar_widget = QWidget()
        self.control_bar_widget.setObjectName("controlBar")
        self.control_bar_widget.setStyleSheet(CONTROL_BAR_STYLE_WINDOWED)
        self.control_bar_widget.setLayout(control_bar)

        self.title_label = QLabel("")
        self.title_label.setStyleSheet("font-size: 15px; font-weight: 600; color: #f2f3f5;")

        # Solo visible para películas (ver _update_nav_buttons): las series
        # ya se renombran desde la lista de episodios (episodes.py), pero
        # una película no pasa por esa vista — este es su único punto de
        # entrada para ponerle un nombre custom.
        self.title_rename_button = QPushButton("✎")
        self.title_rename_button.setObjectName("flatIconButton")
        self.title_rename_button.setFixedWidth(28)
        self.title_rename_button.setToolTip("Renombrar")
        self.title_rename_button.clicked.connect(self._rename_movie)

        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(8)
        title_row.addWidget(self.title_label)
        title_row.addWidget(self.title_rename_button)
        title_row.addStretch()
        self.title_row_container = QWidget()
        self.title_row_container.setLayout(title_row)

        self.audio_selector = QComboBox()
        self.subtitle_selector = QComboBox()
        # Antes solo tenían setMaximumWidth(340) y sin mínimo — sin nada
        # forzando que crezcan, el combo se achicaba a su tamaño natural
        # (mucho más angosto, apenas alcanzaba a mostrar el principio de la
        # descripción de la pista). El mínimo es lo que realmente los
        # ensancha.
        self.audio_selector.setMinimumWidth(320)
        self.subtitle_selector.setMinimumWidth(320)
        self.audio_selector.setMaximumWidth(500)
        self.subtitle_selector.setMaximumWidth(500)
        self.audio_selector.currentIndexChanged.connect(self._on_audio_selected)
        self.subtitle_selector.currentIndexChanged.connect(self._on_subtitle_selected)

        self.audio_use_for_series_button = QPushButton("Usar para toda la serie")
        self.subtitle_use_for_series_button = QPushButton("Usar para toda la serie")
        self.audio_use_for_series_button.clicked.connect(self._save_audio_pref)
        self.subtitle_use_for_series_button.clicked.connect(self._save_subtitle_pref)

        audio_row = QHBoxLayout()
        audio_row.setSpacing(8)
        audio_row.addWidget(QLabel("Audio:"))
        audio_row.addWidget(self.audio_selector)
        audio_row.addWidget(self.audio_use_for_series_button)
        audio_row.addStretch()
        self.audio_row_container = QWidget()
        self.audio_row_container.setLayout(audio_row)

        subtitle_row = QHBoxLayout()
        subtitle_row.setSpacing(8)
        subtitle_row.addWidget(QLabel("Subtítulos:"))
        subtitle_row.addWidget(self.subtitle_selector)
        subtitle_row.addWidget(self.subtitle_use_for_series_button)
        subtitle_row.addStretch()
        self.subtitle_row_container = QWidget()
        self.subtitle_row_container.setLayout(subtitle_row)

        # tracks_container agrupa Audio+Subtítulos lado a lado, tanto
        # para series como para películas (pedido explícito — antes solo
        # películas lo tenían así, series quedaban apiladas).
        self.tracks_container = QWidget()
        self._tracks_layout = QHBoxLayout(self.tracks_container)
        self._tracks_layout.setContentsMargins(0, 0, 0, 0)
        self._tracks_layout.setSpacing(PLAYER_SPACING)
        self._tracks_layout.addWidget(self.audio_row_container)
        self._tracks_layout.addWidget(self.subtitle_row_container)
        self._tracks_layout.addStretch()

        # --- Corrección manual de temporada/categoría (solo series) ---
        # Para cuando el heurístico de smb_scanner._classify_kind()
        # adivina mal (ej. un episodio normal con "Special" en el título
        # narrativo, o un opening sin ningún patrón reconocible en el
        # nombre) — guarda un override server-side (library_metadata,
        # mismo mecanismo que nombres custom) que gana sobre la
        # clasificación automática la próxima vez que se escanee la
        # serie. Colapsado por defecto, como en la referencia del
        # usuario.
        self.correction_toggle_button = QPushButton(CORRECTION_TOGGLE_TEXT_COLLAPSED)
        self.correction_toggle_button.setObjectName("linkButton")
        self.correction_toggle_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.correction_toggle_button.clicked.connect(self._toggle_correction_panel)

        self.correction_season_spin = QSpinBox()
        self.correction_season_spin.setRange(1, 99)

        # 0 = "Auto" (no forzar número, dejar el orden natural por nombre
        # de archivo) — ver _apply_episode_correction/
        # smb_scanner.sorted_entries. setSpecialValueText hace que el 0
        # se vea como texto en vez de como el número 0.
        self.correction_episode_spin = QSpinBox()
        self.correction_episode_spin.setRange(0, 999)
        self.correction_episode_spin.setSpecialValueText("Auto")
        self.correction_episode_spin.setToolTip(
            "Posición del episodio dentro de su temporada/categoría — "
            "\"Auto\" deja el orden automático por nombre de archivo, "
            "un número fuerza esa posición y corre al resto."
        )

        self.correction_category_combo = QComboBox()
        for label, _kind in CORRECTION_CATEGORY_OPTIONS:
            self.correction_category_combo.addItem(label)

        correction_apply_button = QPushButton("Corregir")
        correction_apply_button.clicked.connect(self._apply_episode_correction)

        correction_fields_row = QHBoxLayout()
        correction_fields_row.setSpacing(8)
        correction_fields_row.addWidget(QLabel("Temporada:"))
        correction_fields_row.addWidget(self.correction_season_spin)
        correction_fields_row.addWidget(QLabel("Episodio:"))
        correction_fields_row.addWidget(self.correction_episode_spin)
        correction_fields_row.addWidget(QLabel("Categoría:"))
        correction_fields_row.addWidget(self.correction_category_combo)
        correction_fields_row.addWidget(correction_apply_button)
        correction_fields_row.addStretch()

        self.correction_fields_widget = QWidget()
        self.correction_fields_widget.setLayout(correction_fields_row)
        self.correction_fields_widget.setVisible(False)  # colapsado hasta que se togglee

        # --- Nombre de ordenación manual (solo películas) ---
        # Mismo toggle/container de arriba, pero para el otro caso que no
        # tiene temporada/episodio/categoría: una película. Campo NUNCA
        # visible en la galería, solo decide el orden A-Z/Z-A (ver
        # library_metadata.set_movie_sort_key) — pedido explícito: sagas
        # con numeración romana o subtítulos ("Kiddy Grade: Ignition") no
        # ordenan bien por título real, mismo concepto que el "sort
        # title" de Plex/Jellyfin. Cuál de los dos widgets de campos se
        # muestra (este o correction_fields_widget de arriba) lo decide
        # _refresh_correction_panel según is_series, nunca los dos juntos.
        self.correction_movie_sort_input = QLineEdit()
        self.correction_movie_sort_input.setPlaceholderText(
            'ej. "pelicula 2" — vacío para volver a ordenar por título'
        )
        self.correction_movie_sort_input.setMinimumWidth(260)

        correction_movie_apply_button = QPushButton("Corregir")
        correction_movie_apply_button.clicked.connect(self._apply_movie_sort_key)

        correction_movie_fields_row = QHBoxLayout()
        correction_movie_fields_row.setSpacing(8)
        correction_movie_fields_row.addWidget(QLabel("Orden:"))
        correction_movie_fields_row.addWidget(self.correction_movie_sort_input)
        correction_movie_fields_row.addWidget(correction_movie_apply_button)
        correction_movie_fields_row.addStretch()

        self.correction_movie_fields_widget = QWidget()
        self.correction_movie_fields_widget.setLayout(correction_movie_fields_row)
        self.correction_movie_fields_widget.setVisible(False)  # colapsado hasta que se togglee

        correction_col = QVBoxLayout()
        correction_col.setContentsMargins(0, 0, 0, 0)
        correction_col.setSpacing(6)
        correction_col.addWidget(self.correction_toggle_button)
        correction_col.addWidget(self.correction_fields_widget)
        correction_col.addWidget(self.correction_movie_fields_widget)

        self.correction_container = QWidget()
        self.correction_container.setLayout(correction_col)

        # Los atajos de teclado (espacio, flechas, F) se manejan en
        # keyPressEvent de PlayerView — si estos controles pudieran tomar
        # foco por click, Qt les mandaría la tecla a ELLOS primero (un
        # QPushButton enfocado consume espacio, un QSlider enfocado
        # consume las flechas) y el atajo nunca llegaría acá.
        for widget in (
            self.prev_button, self.play_pause_button, self.next_button,
            self.volume_button, self.fullscreen_button,
            self.seek_slider, self.volume_slider, self.title_rename_button,
        ):
            widget.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        # player_card: video + barra de controles pegados (sin gap), como
        # una sola tarjeta. Tiene ancho Expanding (sin restricción propia
        # de ancho — sigue el ancho de content_column, ver abajo) pero su
        # ALTO se fuerza a mano según el aspect ratio real del video
        # (_update_player_card_geometry). En fullscreen se le sacan esas
        # restricciones para que vuelva a ocupar toda la pantalla (ver
        # enter_fullscreen).
        self.player_card = QWidget()
        self.player_card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._card_layout = QVBoxLayout(self.player_card)
        self._card_layout.setContentsMargins(0, 0, 0, 0)
        self._card_layout.setSpacing(0)
        self._card_layout.addWidget(self.video_frame, stretch=1)
        self._card_layout.addWidget(self.control_bar_widget)

        # content_column agrupa TODO lo que antes vivía directo en
        # self._layout (volver, player_card, título, filas de audio/subs)
        # — así el ancho fijo que calcula _update_player_card_geometry()
        # se aplica acá (una sola vez) y de rebote todo el contenido
        # queda contenido en ese ancho, en vez de que cada fila se
        # estirara sola al ancho completo de la ventana.
        self.content_column = QWidget()
        self.content_column.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        content_layout = QVBoxLayout(self.content_column)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(PLAYER_SPACING)
        content_layout.addWidget(self.player_card, stretch=1)
        content_layout.addWidget(self.title_row_container)
        content_layout.addWidget(self.tracks_container)
        content_layout.addWidget(self.correction_container)
        self._content_layout = content_layout

        # content_column entre dos addStretch() para centrarlo — PERO con
        # un stretch factor exagerado a su favor (1000 contra 1 de cada
        # spacer). Sin esto (probado, no alcanza con stretch=0 + Expanding
        # sizePolicy por default): en fullscreen, con content_column sin
        # límite de ancho propio, Qt reparte el espacio sobrante en 3
        # partes IGUALES entre los dos addStretch() y content_column
        # (bug real encontrado: quedaba angosto, un tercio de la pantalla,
        # en el medio) — un QSizePolicy.Expanding sin stretch factor
        # explícito compite en igualdad de condiciones con addStretch(),
        # no gana. Con 1000 contra 1, content_column se queda con
        # prácticamente TODO el sobrante en fullscreen (~99.8%, el resto
        # es una fracción de píxel invisible en los spacers). En modo
        # ventana, apenas content_column llega a su setFixedWidth() (ver
        # _update_player_card_geometry), no puede crecer más sin importar
        # su stretch factor, y el sobrante se reparte parejo entre los dos
        # spacers — quedando centrado igual que antes.
        center_row = QHBoxLayout()
        center_row.setContentsMargins(0, 0, 0, 0)
        center_row.addStretch(1)
        center_row.addWidget(self.content_column, 1000)
        center_row.addStretch(1)
        self.center_row = center_row

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(*PLAYER_MARGINS)
        # PLAYER_SPACING acá (antes 0, no hacía falta con un solo ítem) es
        # el espacio entre header_bar y el video/tarjeta de abajo — ver
        # _update_player_card_geometry, que lo tiene en cuenta al calcular
        # cuánto alto le queda disponible al video.
        self._layout.setSpacing(PLAYER_SPACING)
        self._layout.addWidget(self.header_bar)
        self._layout.addLayout(center_row, stretch=1)

        self.video_frame.clicked.connect(self._toggle_play_pause)
        self.video_frame.mouse_moved.connect(self._on_video_mouse_moved)
        self.video_frame.resized.connect(self._reposition_overlay_controls)
        self.video_frame.resized.connect(self._update_video_mask)

        self._fullscreen_active = False
        self._cursor_hidden = False
        self._hide_controls_timer = QTimer(self)
        self._hide_controls_timer.setInterval(HIDE_CONTROLS_DELAY_MS)
        self._hide_controls_timer.setSingleShot(True)
        self._hide_controls_timer.timeout.connect(self._hide_fullscreen_controls)

        self._suppress_end_event = False  # ver stop_playback(): evita reaccionar a un end-of-stream tardío tras un stop() manual
        self._engine.media_playing.connect(self._on_media_playing_qt)
        self._engine.media_end_reached.connect(self._on_media_end_qt)
        self._engine.media_failed.connect(self._on_media_failed_qt)

        self._progress_timer = QTimer(self)
        self._progress_timer.setInterval(PROGRESS_SAVE_INTERVAL_MS)
        self._progress_timer.timeout.connect(self._save_progress)

        self._ui_timer = QTimer(self)
        self._ui_timer.setInterval(UI_REFRESH_INTERVAL_MS)
        self._ui_timer.timeout.connect(self._update_ui_position)

        self._current_library = None  # objeto Library completo (a diferencia de _current_library_id) — lo necesita _rename_movie para pegarle a library_metadata
        self._current_library_id = None
        self._current_subpath = None
        self._resume_position_ms = None
        self._base_title = ""
        self._play_started_at = time.monotonic()
        self._rename_worker = None
        self._correction_expanded = False
        self._movie_sort_prefilled = False

        self._update_nav_buttons()
        self._update_player_card_geometry()
        self._update_video_mask()

    def play_url(self, url, title, library_id, subpath, extra_options=(),
                 playlist=None, playlist_index=-1, series_key=None, library=None):
        self._suppress_end_event = False
        self._play_started_at = time.monotonic()
        self._base_title = title
        self.title_label.setText(title)
        self.audio_selector.clear()
        self.subtitle_selector.clear()
        self.seek_slider.setRange(0, 0)

        self._current_library = library
        self._current_library_id = library_id
        self._current_subpath = subpath
        self._resume_position_ms = progress_store.get_progress_ms(library, subpath) if library else None

        self.playlist = playlist or []
        self.playlist_index = playlist_index
        self.series_key = series_key

        # El panel de corrección se resetea a colapsado/valores por
        # defecto en cada episodio/película nueva — no intenta reflejar
        # la clasificación actual (auto o con override), es una
        # herramienta de "poner el valor correcto", no de "confirmar el
        # actual". Única excepción: el campo de orden de películas SÍ se
        # precarga con el valor actual, pero recién al expandir el panel
        # (ver _toggle_correction_panel/_prefill_movie_sort_key) — acá
        # solo se limpia y se marca para volver a precargar.
        self._correction_expanded = False
        self._movie_sort_prefilled = False
        self.correction_movie_sort_input.clear()
        self.correction_season_spin.setValue(1)
        self.correction_episode_spin.setValue(0)
        self.correction_category_combo.setCurrentIndex(0)
        self._update_nav_buttons()  # deja correction_container/toggle en el estado colapsado recién seteado

        self._engine.play_url(url, extra_options, int(self.video_frame.winId()))
        self._progress_timer.start()
        self._ui_timer.start()
        self.setFocus()  # para que espacio/flechas/F funcionen apenas arranca a reproducir

    def _update_nav_buttons(self):
        # series_key solo viene seteado desde _play_episode/
        # _play_episode_from_player (ver main.py MainWindow) — None
        # significa película, que no tiene episodios ni concepto de
        # "serie" para guardar una preferencia de pista. Se OCULTAN (no
        # solo deshabilitan) porque no tienen ningún sentido ahí, no son
        # solo "no disponibles por ahora".
        is_series = self.series_key is not None
        self.prev_button.setVisible(is_series)
        self.next_button.setVisible(is_series)
        if is_series:
            # "anterior" siempre hace algo útil (reinicia el episodio
            # actual como mínimo), así que va siempre habilitado — ver
            # _play_previous.
            self.prev_button.setEnabled(True)
            self.next_button.setEnabled(0 <= self.playlist_index < len(self.playlist) - 1)
        self.audio_use_for_series_button.setVisible(is_series)
        self.subtitle_use_for_series_button.setVisible(is_series)
        self.title_rename_button.setVisible(not is_series)
        self._refresh_correction_panel()

    def _refresh_correction_panel(self):
        """Decide visibilidad/texto del panel de corrección compartido
        (correction_container) según haya o no contenido cargado y sea
        serie o película — llamado desde _update_nav_buttons (cambia
        is_series) y desde _toggle_correction_panel (cambia
        _correction_expanded). El container entero se oculta sin
        contenido cargado (estado inicial de PlayerView, antes del
        primer play_url); con contenido, se muestra siempre — a
        diferencia de antes (Paso 16), que lo escondía por completo para
        películas al no tener nada que corregir ahí todavía.

        También se oculta en fullscreen aunque haya contenido (bug real:
        avanzar de episodio en pantalla completa vuelve a llamar a
        play_url() → _update_nav_buttons() → acá, sin pasar por
        enter_fullscreen() otra vez — sin este chequeo, el panel
        reaparecía empujando el layout y deformando el video)."""
        has_content = self._current_library is not None and not self._fullscreen_active
        is_series = self.series_key is not None
        self.correction_container.setVisible(has_content)
        self.correction_fields_widget.setVisible(has_content and is_series and self._correction_expanded)
        self.correction_movie_fields_widget.setVisible(
            has_content and not is_series and self._correction_expanded
        )
        if is_series:
            collapsed, expanded = CORRECTION_TOGGLE_TEXT_COLLAPSED, CORRECTION_TOGGLE_TEXT_EXPANDED
        else:
            collapsed, expanded = CORRECTION_TOGGLE_TEXT_MOVIE_COLLAPSED, CORRECTION_TOGGLE_TEXT_MOVIE_EXPANDED
        self.correction_toggle_button.setText(expanded if self._correction_expanded else collapsed)

    def _play_previous(self):
        action, subpath, title = player_engine.resolve_previous_action(
            self.media_player, self.playlist, self.playlist_index
        )
        if action == "change":
            self.episode_change_requested.emit(subpath, title)
        else:
            self.media_player.set_time(0)

    def _play_next(self):
        if 0 <= self.playlist_index < len(self.playlist) - 1:
            subpath, title = self.playlist[self.playlist_index + 1][:2]
            self.episode_change_requested.emit(subpath, title)

    def _skip(self, delta_ms):
        length = self.media_player.get_length() or 0
        position = self.media_player.get_time() or 0
        new_position = max(0, position + delta_ms)
        if length > 0:
            new_position = min(new_position, length)
        self.media_player.set_time(new_position)

    def keyPressEvent(self, event):
        if self._fullscreen_active:
            self._restart_hide_timer()  # cualquier tecla revela los controles, no solo mouse

        key = event.key()
        if key == Qt.Key.Key_Space:
            self._toggle_play_pause()
        elif key == Qt.Key.Key_Right:
            self._skip(SKIP_MS)
        elif key == Qt.Key.Key_Left:
            self._skip(-SKIP_MS)
        elif key == Qt.Key.Key_F:
            self.fullscreen_requested.emit()
        elif key == Qt.Key.Key_Escape and self._fullscreen_active:
            self.fullscreen_requested.emit()
        else:
            super().keyPressEvent(event)

    # --- Fullscreen: solo el video, con controles flotantes que se
    # autoocultan (ver Paso 9 en CONTEXT.md para el detalle completo de
    # por qué está armado así) ---

    def enter_fullscreen(self):
        self._fullscreen_active = True

        # Sin esto el video queda inset por los márgenes normales del
        # layout — se ve como un borde gris (el fondo de la app) alrededor
        # del video en vez de ocupar la pantalla completa.
        self._layout.setContentsMargins(0, 0, 0, 0)

        # content_column (ancho fijo) y player_card (alto fijo) tenían
        # restricciones de tamaño puestas por _update_player_card_geometry
        # (solo aplica en modo ventana) — hay que sacárselas para que
        # puedan volver a ocupar toda la pantalla. IMPORTANTE: esto tiene
        # que pasar ANTES de que la ventana realmente cambie de tamaño
        # (ver MainWindow._toggle_fullscreen, que por eso llama a este
        # método ANTES de tocar el estado de la ventana) — si no, el
        # primer resizeEvent con las dimensiones de fullscreen llega
        # mientras _fullscreen_active todavía es False y recalcula un
        # tamaño fijo aspect-ratio otra vez, dejando el video como una
        # franja angosta en el medio de la pantalla.
        for widget in (self.content_column, self.player_card):
            widget.setMinimumSize(0, 0)
            widget.setMaximumSize(WIDGET_SIZE_MAX, WIDGET_SIZE_MAX)
        self._update_video_mask()  # sin máscara de esquinas en fullscreen

        self.header_bar.hide()
        self.title_row_container.hide()
        self.tracks_container.hide()
        self.correction_container.hide()

        # El control bar deja de ser parte del layout normal (debajo del
        # video, adentro de player_card) y pasa a ser un hijo del propio
        # video_frame, posicionado a mano con setGeometry() y flotando
        # encima — WA_NativeWindow fuerza a que tenga su propia ventana
        # X11 hija, para que componga arriba del contenido que dibuja
        # libVLC ahí adentro (que no pasa por el sistema de pintado normal
        # de Qt).
        self._card_layout.removeWidget(self.control_bar_widget)
        self.control_bar_widget.setParent(self.video_frame)
        self.control_bar_widget.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.control_bar_widget.setStyleSheet(CONTROL_BAR_STYLE_FULLSCREEN)
        self._reposition_overlay_controls()
        self.control_bar_widget.show()
        self.control_bar_widget.raise_()

        self._restart_hide_timer()

    def exit_fullscreen(self):
        self._fullscreen_active = False
        self._hide_controls_timer.stop()
        if self._cursor_hidden:
            QApplication.restoreOverrideCursor()
            self._cursor_hidden = False

        self._layout.setContentsMargins(*PLAYER_MARGINS)

        self.control_bar_widget.setParent(None)
        self.control_bar_widget.setStyleSheet(CONTROL_BAR_STYLE_WINDOWED)
        self._card_layout.addWidget(self.control_bar_widget)
        self.control_bar_widget.show()

        self.header_bar.show()
        self.title_row_container.show()
        self.tracks_container.show()
        self._refresh_correction_panel()

        self._update_player_card_geometry()
        self._update_video_mask()

    def _reposition_overlay_controls(self):
        if not self._fullscreen_active:
            return
        self.control_bar_widget.setGeometry(
            0, self.video_frame.height() - OVERLAY_BAR_HEIGHT,
            self.video_frame.width(), OVERLAY_BAR_HEIGHT,
        )

    def _update_video_mask(self):
        # video_frame dibuja contenido NATIVO (libVLC vía set_xwindow(),
        # por fuera del sistema de pintado de Qt) — un border-radius por
        # QSS no lo recorta (ver Paso 6 en CONTEXT.md). setMask() con una
        # QRegion sí funciona igual, porque actúa a nivel de la ventana
        # X11 (XShape), por debajo de quién dibuja adentro. Esquinas de
        # ARRIBA redondeadas (calzan con el radio de abajo de
        # CONTROL_BAR_STYLE_WINDOWED, pegado sin gap — ver player_card),
        # esquinas de ABAJO cuadradas (el video sigue directo hacia la
        # barra de controles). En fullscreen no hay máscara — video a
        # pantalla completa, sin recortar nada.
        if self._fullscreen_active:
            self.video_frame.clearMask()
            return
        w, h = self.video_frame.width(), self.video_frame.height()
        if w <= 0 or h <= 0:
            return
        r = VIDEO_TOP_RADIUS
        path = QPainterPath()
        path.moveTo(0, h)
        path.lineTo(0, r)
        path.quadTo(0, 0, r, 0)
        path.lineTo(w - r, 0)
        path.quadTo(w, 0, w, r)
        path.lineTo(w, h)
        path.closeSubpath()
        self.video_frame.setMask(QRegion(path.toFillPolygon().toPolygon()))

    def _on_video_mouse_moved(self):
        if self._fullscreen_active:
            self._restart_hide_timer()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_player_card_geometry()

    def _update_player_card_geometry(self):
        # Solo en modo ventana: en fullscreen ni content_column ni
        # player_card tienen restricción de tamaño (ver enter_fullscreen)
        # y ocupan toda la pantalla como antes.
        if self._fullscreen_active:
            return

        control_bar_h = self.control_bar_widget.sizeHint().height()
        # correction_container está oculto hasta el primer play_url() (ver
        # _refresh_correction_panel) — sizeHint() ignora visibilidad, así
        # que hay que descartarlo a mano acá (si no, se reserva espacio de
        # más para un panel que no se está mostrando).
        correction_visible = self.correction_container.isVisible()
        correction_h = self.correction_container.sizeHint().height() if correction_visible else 0
        # header_bar YA NO es parte de content_layout (ver __init__: ahora
        # vive afuera, directo en self._layout, para que ocupe ancho
        # completo independiente del ancho de content_column) — su alto
        # se resta aparte más abajo (header_h), no acá adentro de chrome_h.
        chrome_h = (
            self.title_row_container.sizeHint().height()
            + self.tracks_container.sizeHint().height()
            + correction_h
        )
        margins = self._layout.contentsMargins()
        # content_layout tiene 4 ítems (player_card, title,
        # tracks_container, correction_container) cuando este último está
        # visible (3 huecos), o 3 ítems cuando no (2 huecos, solo antes
        # del primer play_url()).
        visible_chrome_items = 3 if correction_visible else 2
        spacing_total = visible_chrome_items * self._content_layout.spacing()
        # header_bar + el espacio entre él y center_row (self._layout.spacing()).
        header_h = self.header_bar.sizeHint().height() + self._layout.spacing()
        available_h = self.height() - margins.top() - margins.bottom() - chrome_h - spacing_total - header_h
        available_w = self.width() - margins.left() - margins.right()
        if available_h <= 0 or available_w <= 0:
            return

        video_h = max(MIN_VIDEO_HEIGHT, available_h - control_bar_h)
        video_w = video_h * self._video_aspect_ratio
        if video_w > available_w:
            video_w = available_w
            video_h = video_w / self._video_aspect_ratio

        # El límite de ancho va en content_column (así arrastra con él a
        # top_row/título/filas de audio-subs, ver su construcción en
        # __init__) — player_card solo necesita el límite de ALTO, su
        # ancho ya sigue el de content_column vía el layout (Expanding).
        #
        # setMaximum*, NO setFixed*: setFixedWidth/Height fijan mínimo Y
        # máximo, y ese MÍNIMO se propaga hacia arriba como mínimo de toda
        # la ventana (QStackedLayout toma el máximo de los mínimos de sus
        # hijos). Como este método corre desde exit_fullscreen() cuando la
        # ventana TODAVÍA mide el alto de pantalla completa, el mínimo
        # quedaba en ese alto y después la ventana ya no podía achicarse
        # al área disponible: setGeometry() se ignoraba en silencio y la
        # ventana terminaba tapando el panel de Plasma. Confirmado con
        # test: MainWindow.minimumSizeHint llegaba a 987 (alto de pantalla)
        # en vez de ~400. Con máximo solamente, el layout igual estira
        # ambos widgets hasta el límite (los dos son Expanding y tienen
        # stretch), así que se ven igual, pero sin imponer ningún mínimo.
        self.content_column.setMaximumWidth(max(200, round(video_w)))
        self.player_card.setMaximumHeight(round(video_h) + control_bar_h)

    def _restart_hide_timer(self):
        self.control_bar_widget.show()
        self.control_bar_widget.raise_()
        if self._cursor_hidden:
            QApplication.restoreOverrideCursor()
            self._cursor_hidden = False
        self._hide_controls_timer.start()

    def _hide_fullscreen_controls(self):
        if not self._fullscreen_active:
            return
        self.control_bar_widget.hide()
        QApplication.setOverrideCursor(Qt.CursorShape.BlankCursor)
        self._cursor_hidden = True

    def _toggle_play_pause(self):
        if self.media_player.get_state() == vlc.State.Playing:
            self.media_player.pause()
            self._save_progress()
            # Async, SIN esperar (ver progress_store.checkpoint) — pausar
            # tiene que sentirse instantáneo. El guardado en sí ya quedó
            # a salvo en disco local con _save_progress(); esto solo
            # programa la subida al target centralizado (si corresponde)
            # en un QThread de fondo.
            if self._current_library is not None:
                progress_store.checkpoint(self._current_library)
        else:
            self.media_player.play()

    def _toggle_mute(self):
        muted = self.media_player.audio_get_mute()
        self.media_player.audio_set_mute(not muted)
        self.volume_button.set_icon_name("volume_muted" if not muted else "volume")

    def _on_seek_pressed(self):
        self._seek_dragging = True

    def _on_seek_released(self):
        self._seek_dragging = False
        self.media_player.set_time(self.seek_slider.value())

    def stop_playback(self):
        # media_player.stop() puede disparar MediaPlayerEndReached de forma
        # asincrónica — sin este guard, un stop manual (botón "Volver")
        # podía hacer que el auto-avance reaccionara tarde y reprodujera el
        # siguiente episodio o volviera a emitir back_requested después de
        # que el usuario ya navegó a otro lado.
        self._suppress_end_event = True
        self._progress_timer.stop()
        self._ui_timer.stop()
        self._save_progress()
        # Bloqueante CON tope (a diferencia de la pausa): "Volver" tiene
        # que darle al último guardado una chance real de llegar al
        # target centralizado antes de dejar avanzar a la lista de
        # episodios/galería (para una biblioteca no centralizada esto es
        # no-op, la escritura local ya fue síncrona) — ver
        # progress_store.checkpoint/centralized_store.RemoteJsonStore.wait_pending.
        if self._current_library is not None:
            progress_store.checkpoint(self._current_library, blocking=True)
        self.media_player.stop()
        self._current_library = None
        self._current_library_id = None
        self._current_subpath = None

    def _save_progress(self):
        if self._current_library is None:
            return
        position_ms = self.media_player.get_time()
        duration_ms = self.media_player.get_length()
        progress_store.save_progress(
            self._current_library, self._current_subpath, position_ms, duration_ms
        )

    def _save_progress_and_wait(self):
        """Usado por app.aboutToQuit (ver MainWindow más abajo) — a
        diferencia del guardado periódico de cada 5s (que nunca debe
        bloquear la UI), acá sí vale la pena esperar un poco a que el
        flush a un target centralizado termine antes de que el proceso
        termine, o se pierde sistemáticamente el último tramo."""
        self._save_progress()
        if self._current_library is not None:
            progress_store.checkpoint(self._current_library, blocking=True)

    def _update_ui_position(self):
        length = self.media_player.get_length()
        position = self.media_player.get_time()
        if length and length > 0:
            if self.seek_slider.maximum() != length:
                self.seek_slider.setRange(0, length)
            self.duration_label.setText(_format_time(length))
        if not self._seek_dragging:
            self.seek_slider.blockSignals(True)
            self.seek_slider.setValue(max(0, position))
            self.seek_slider.blockSignals(False)
            self.elapsed_label.setText(_format_time(position))
        is_playing = self.media_player.get_state() == vlc.State.Playing
        self.play_pause_button.set_icon_name("pause" if is_playing else "play")

    def _on_media_end_qt(self):
        if self._suppress_end_event:
            return
        result = player_engine.resolve_auto_advance(
            self.media_player, self.playlist, self.playlist_index, self._play_started_at
        )
        if result is None:
            return  # película u otro caso sin playlist: sin auto-avance
        action, subpath, title = result
        if action == "advance":
            self.episode_change_requested.emit(subpath, title)
        else:
            self.back_requested.emit()

    def _on_media_failed_qt(self, message):
        # No hay nada que guardar acá (_save_progress no-opea con
        # duration_ms<=0, ver progress_store.save_progress) — el video
        # nunca llegó a reproducirse de verdad.
        self._progress_timer.stop()
        self._ui_timer.stop()
        QMessageBox.warning(self, "No se pudo reproducir", message)
        self.back_requested.emit()

    def _on_media_playing_qt(self):
        # Ver player_engine.TRACKS_REFRESH_RETRY_DELAYS_MS — varios
        # intentos en vez de uno solo, la lista de pistas puede tardar más
        # de un intento en estar completa sobre una red más lenta.
        for delay_ms in player_engine.TRACKS_REFRESH_RETRY_DELAYS_MS:
            QTimer.singleShot(delay_ms, self._refresh_tracks)
        self._refresh_video_aspect_ratio()

        if self._resume_position_ms:
            self.media_player.set_time(self._resume_position_ms)
            self.title_label.setText(
                f"{self._base_title} (retomando desde {_format_time(self._resume_position_ms)})"
            )
            # Solo la primera vez: si el usuario pausa y reanuda con el botón
            # de play no hay que re-seekear.
            self._resume_position_ms = None

    def _refresh_video_aspect_ratio(self):
        # video_get_size() recién devuelve algo útil una vez que el demux
        # decodificó el primer frame — puede no estar listo todavía justo
        # al entrar en Playing, por eso el fallback silencioso a lo que ya
        # había (el default 16:9, o el de un episodio anterior de la misma
        # serie) si todavía da (0, 0).
        width, height = self.media_player.video_get_size(0)
        if width and height:
            self._video_aspect_ratio = width / height
            self._update_player_card_geometry()

    def _refresh_tracks(self):
        audio_descriptions = self.media_player.audio_get_track_description()
        subtitle_descriptions = self.media_player.video_get_spu_description()

        self._populate_track_selector(
            self.audio_selector, audio_descriptions, self.media_player.audio_get_track()
        )
        self._populate_track_selector(
            self.subtitle_selector, subtitle_descriptions, self.media_player.video_get_spu()
        )

        if self.series_key and self._current_library is not None:
            prefs = track_prefs.get_prefs(self._current_library, self.series_key[1])
            audio_id = track_prefs.find_matching_track(audio_descriptions, prefs.get("audio"))
            if audio_id is not None:
                self._select_combo_track(self.audio_selector, audio_id)
            subtitle_id = track_prefs.find_matching_track(subtitle_descriptions, prefs.get("subtitle"))
            if subtitle_id is not None:
                self._select_combo_track(self.subtitle_selector, subtitle_id)

    def _select_combo_track(self, combo, track_id):
        for i in range(combo.count()):
            if combo.itemData(i) == track_id:
                combo.setCurrentIndex(i)  # dispara _on_audio_selected/_on_subtitle_selected normalmente
                break

    def _populate_track_selector(self, combo, descriptions, current_track_id):
        options, current_index = player_engine.build_track_picker_options(descriptions, current_track_id)
        combo.blockSignals(True)
        combo.clear()
        for label, track_id in options:
            combo.addItem(label, track_id)
        combo.setCurrentIndex(current_index)
        combo.blockSignals(False)

    def _on_audio_selected(self, index):
        track_id = self.audio_selector.itemData(index)
        if track_id is not None:
            self.media_player.audio_set_track(track_id)

    def _on_subtitle_selected(self, index):
        track_id = self.subtitle_selector.itemData(index)
        if track_id is not None:
            self.media_player.video_set_spu(track_id)

    def _save_audio_pref(self):
        if not self.series_key or self._current_library is None:
            return
        track_prefs.set_audio_pref(self._current_library, self.series_key[1], self.audio_selector.currentText())

    def _save_subtitle_pref(self):
        if not self.series_key or self._current_library is None:
            return
        track_prefs.set_subtitle_pref(self._current_library, self.series_key[1], self.subtitle_selector.currentText())

    def _rename_movie(self):
        # Mismo mecanismo que _rename_series en episodes.py (library_metadata,
        # guardado server-side), pero para películas — que no pasan por
        # esa vista, así que este es su único punto de entrada.
        if not self._current_library:
            return
        password = credentials.get_password(self._current_library.id, self._current_library.username) or ""
        # smb_scanner.stem(), NO subpath[0] a secas: subpath es
        # (nombre_de_archivo,) CON extensión, pero la key bajo la que
        # ScanWorker guarda/lee nombre/orden custom de una película es el
        # STEM sin extensión (MovieEntry.name, ver smb_scanner.scan_movies)
        # — bug real encontrado en vivo probando el campo de orden antes
        # de compilar el Flatpak (Paso 36): con subpath[0] a secas, el
        # nombre se guardaba bien pero bajo una key que el próximo escaneo
        # nunca vuelve a leer, así que se perdía en cuanto la app se
        # reabría o se apretaba "Actualizar biblioteca" — parecía andar
        # en el momento solo porque movie_renamed parchea el nombre en
        # memoria de la MISMA sesión (ver patch_series_name/
        # update_series_name), sin pegarle al JSON de vuelta.
        movie_key = smb_scanner.stem(self._current_subpath[0])
        # Llamada síncrona en el thread principal (a diferencia de
        # episodes.py._rename_series, que reusa un nombre ya cargado antes
        # — acá no hay ningún "antes", este es el único punto de entrada
        # para películas) — library_metadata._load() puede relanzar
        # StorageError ante un hipo de red real (ver
        # library_metadata._read_json). Degradado a secas: si falla, el
        # diálogo arranca vacío en vez de precargado con el nombre custom
        # actual — no es destructivo, solo cosmético.
        try:
            current = library_metadata.get_raw_movie_name(self._current_library, password, movie_key) or ""
        except smb_scanner.StorageError:
            current = ""
        text, ok = QInputDialog.getText(
            self, "Renombrar película",
            "Nombre (vacío para volver al nombre de archivo):",
            text=current,
        )
        if not ok:
            return
        text = text.strip()
        library, subpath = self._current_library, self._current_subpath
        self._rename_worker = MetadataSaveWorker(
            lambda: library_metadata.set_movie_name(library, password, movie_key, text)
        )
        self._rename_worker.finished_ok.connect(lambda: self._on_movie_renamed(library, subpath, text or movie_key))
        self._rename_worker.failed.connect(self._on_movie_rename_failed)
        self._rename_worker.start()

    def _on_movie_renamed(self, library, subpath, new_name):
        self._base_title = new_name
        self.title_label.setText(new_name)
        self.movie_renamed.emit(library, subpath, new_name)

    def _on_movie_rename_failed(self, message):
        QMessageBox.warning(self, "Error", message)

    def _toggle_correction_panel(self):
        self._correction_expanded = not self._correction_expanded
        # Precarga perezosa: recién al expandir el panel de una película
        # por primera vez esta reproducción, no en cada play_url() (que
        # pegaría contra el sidecar de metadata en cada episodio/película
        # nueva sin que el usuario haya pedido nada todavía).
        if self._correction_expanded and self.series_key is None and not self._movie_sort_prefilled:
            self._prefill_movie_sort_key()
        self._refresh_correction_panel()
        self._update_player_card_geometry()

    def _apply_episode_correction(self):
        # Solo series (el botón vive oculto para películas, ver
        # _refresh_correction_panel) — series_key = (library_id, series_root).
        if not self._current_library or not self.series_key:
            return
        _library_id, series_root = self.series_key
        kind = CORRECTION_CATEGORY_OPTIONS[self.correction_category_combo.currentIndex()][1]
        season = None if kind == "special" else self.correction_season_spin.value()
        episode_number = self.correction_episode_spin.value() or None  # 0 ("Auto") -> None

        password = credentials.get_password(self._current_library.id, self._current_library.username) or ""
        library, subpath = self._current_library, self._current_subpath
        self._correction_worker = MetadataSaveWorker(
            lambda: library_metadata.set_episode_override(
                library, password, series_root, subpath, season, kind, episode_number,
            )
        )
        self._correction_worker.finished_ok.connect(self._on_correction_saved)
        self._correction_worker.failed.connect(lambda message: QMessageBox.warning(self, "Error", message))
        self._correction_worker.start()

    def _on_correction_saved(self):
        QMessageBox.information(
            self, "Corrección guardada",
            "Se va a reflejar la próxima vez que abras la lista de episodios de esta serie.",
        )

    def _prefill_movie_sort_key(self):
        # Mismo mecanismo síncrono que _rename_movie (única otra
        # corrección de película, mismo motivo: no hay ningún "antes"
        # cargado para películas, este panel es el único punto de
        # entrada) — a diferencia del panel de episodios de arriba
        # (deliberadamente en blanco, "poner el valor correcto" no
        # "confirmar el actual"), acá SÍ tiene sentido precargar: es un
        # único campo de texto que mapea 1 a 1 con el valor guardado, no
        # un estado compuesto difícil de reflejar.
        self._movie_sort_prefilled = True
        if not self._current_library:
            return
        password = credentials.get_password(self._current_library.id, self._current_library.username) or ""
        movie_key = smb_scanner.stem(self._current_subpath[0])  # ver comentario en _rename_movie
        try:
            current = library_metadata.get_raw_movie_sort_key(self._current_library, password, movie_key) or ""
        except smb_scanner.StorageError:
            current = ""  # degradado a secas, ver _rename_movie
        self.correction_movie_sort_input.setText(current)

    def _apply_movie_sort_key(self):
        # Solo películas (el botón vive oculto para series, ver
        # _refresh_correction_panel).
        if not self._current_library or self.series_key is not None:
            return
        text = self.correction_movie_sort_input.text().strip()
        password = credentials.get_password(self._current_library.id, self._current_library.username) or ""
        library, subpath = self._current_library, self._current_subpath
        movie_key = smb_scanner.stem(subpath[0])  # ver comentario en _rename_movie
        self._correction_worker = MetadataSaveWorker(
            lambda: library_metadata.set_movie_sort_key(library, password, movie_key, text)
        )
        self._correction_worker.finished_ok.connect(self._on_movie_sort_key_saved)
        self._correction_worker.failed.connect(lambda message: QMessageBox.warning(self, "Error", message))
        self._correction_worker.start()

    def _on_movie_sort_key_saved(self):
        # A diferencia de la lista de episodios (episodes.py, siempre
        # re-escanea, sin caché), GalleryView SÍ cachea los ítems por
        # biblioteca (_items_cache) — con solo volver a la galería o
        # cambiar de pestaña y volver, se re-renderiza desde esa caché
        # SIN pegarle a disco de nuevo, así que el orden nuevo no se ve
        # todavía (confirmado en vivo probando este mismo mensaje: "abrí
        # la galería" no alcanza). Hace falta un rescan real —
        # "Actualizar biblioteca" o reabrir la app.
        QMessageBox.information(
            self, "Orden guardado",
            "Se va a reflejar la próxima vez que actualices esta biblioteca "
            "(\"Actualizar biblioteca\") o vuelvas a abrir la app.",
        )


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Lunyx")
        self.resize(1200, 780)

        self.gallery_view = GalleryView()
        self.episode_view = EpisodeListView(on_back=self._show_gallery)
        self.player_view = PlayerView()

        self.stack = QStackedWidget()
        self.stack.addWidget(self.gallery_view)
        self.stack.addWidget(self.episode_view)
        self.stack.addWidget(self.player_view)

        # Aviso de "no se pudo guardar progreso/preferencias" — un solo
        # punto de conexión acá arriba, sin importar en qué vista esté el
        # usuario cuando un target de progreso centralizado se cae (ver
        # centralized_store.notifier). Oculto por defecto; también se
        # oculta explícitamente en fullscreen (ver _toggle_fullscreen) para
        # no romper la promesa de "solo video en pantalla" del Paso 9.
        self.offline_banner = QLabel("")
        self.offline_banner.setStyleSheet(
            "background:#4a2020; color:#f2a0a0; padding:4px 12px; font-size:12px;"
        )
        self.offline_banner.setVisible(False)
        self._offline_banner_pending = False  # True si hay un aviso "vivo" que fullscreen tapó momentáneamente

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.offline_banner)
        layout.addWidget(self.stack)

        centralized_store.notifier.offline_changed.connect(self._on_storage_offline)

        self._series_library = None  # biblioteca de la serie actualmente abierta en episode_view
        self._player_return_view = self.gallery_view
        self._was_maximized_before_fullscreen = False
        # subpath del episodio a saltar apenas termine el scan en curso —
        # ver _resume_series_episode/_on_episode_list_ready. None = no hay
        # ningún salto pendiente, un load() normal no dispara nada.
        self._pending_resume_subpath = None

        self.gallery_view.play_requested.connect(self._play_movie)
        self.gallery_view.series_requested.connect(self._open_series)
        self.gallery_view.resume_episode_requested.connect(self._resume_series_episode)
        self.episode_view.episode_selected.connect(self._play_episode)
        self.episode_view.series_renamed.connect(self.gallery_view.update_series_name)
        self.episode_view.episodes_ready.connect(self._on_episode_list_ready)
        self.player_view.back_requested.connect(self._player_back)
        self.player_view.fullscreen_requested.connect(self._toggle_fullscreen)
        self.player_view.episode_change_requested.connect(self._play_episode_from_player)
        # update_series_name matchea por subpath sin importar si es serie o
        # película (ver gallery.py) — mismo parche en memoria sin rescan.
        self.player_view.movie_renamed.connect(self.gallery_view.update_series_name)

    def _open_series(self, library, subpath, name):
        self._series_library = library
        self._pending_resume_subpath = None  # navegación normal — cancela cualquier salto pendiente de antes
        self.stack.setCurrentWidget(self.episode_view)
        self.episode_view.load(library, subpath, name)

    def _resume_series_episode(self, library, series_subpath, episode_subpath, fallback_title):
        # "Continuar viendo" (galería) pidió un salto DIRECTO al episodio,
        # no a la lista — se carga la lista igual (necesaria para tener
        # flat_episodes/series_key con contexto de playlist real, ver
        # _play_episode), pero apenas termina el scan (episodes_ready) se
        # dispara el play automáticamente en vez de esperar un click.
        self._series_library = library
        self._pending_resume_subpath = episode_subpath
        self.stack.setCurrentWidget(self.episode_view)
        self.episode_view.load(library, series_subpath, fallback_title)

    def _on_episode_list_ready(self):
        if not self._pending_resume_subpath:
            return
        subpath = self._pending_resume_subpath
        self._pending_resume_subpath = None
        match = next((item for item in self.episode_view.flat_episodes if item[0] == subpath), None)
        if match:
            self._play_episode(match[0], match[1])
        # si no está (ej. se corrigió/movió desde entonces) se queda
        # mostrando la lista, sin romper nada — mejor que un salto a un
        # episodio que ya no es el que corresponde.

    def _play_movie(self, library, subpath, name):
        self._player_return_view = self.gallery_view
        self._play(library, subpath, title=name)

    def _play_episode(self, subpath, title):
        self._player_return_view = self.episode_view
        playlist = self.episode_view.flat_episodes
        index = next((i for i, item in enumerate(playlist) if item[0] == subpath), -1)
        series_key = (self._series_library.id, subpath[0])
        self._play(
            self._series_library, subpath, title=title,
            playlist=playlist, playlist_index=index, series_key=series_key,
        )

    def _play_episode_from_player(self, subpath, title):
        # El usuario clickeó anterior/siguiente (o terminó un episodio y
        # auto-avanzó) — el playlist y la serie ya están cargados, solo
        # hay que re-resolver la URL del nuevo episodio.
        playlist = self.player_view.playlist
        index = next((i for i, item in enumerate(playlist) if item[0] == subpath), -1)
        self._play(
            self._series_library, subpath, title=title,
            playlist=playlist, playlist_index=index, series_key=self.player_view.series_key,
        )

    def _play(self, library, subpath, title, playlist=None, playlist_index=-1, series_key=None):
        password = credentials.get_password(library.id, library.username) or ""
        url, options = smb_scanner.playback_target(library, password, *subpath)
        self.stack.setCurrentWidget(self.player_view)
        self.player_view.play_url(
            url, title=title, library_id=library.id, subpath=subpath,
            extra_options=options, playlist=playlist, playlist_index=playlist_index,
            series_key=series_key, library=library,
        )

    def _player_back(self):
        # back_requested también lo dispara el auto-avance (al terminar
        # el último episodio de la temporada/serie, ver
        # PlayerView._on_media_end_qt) — no solo el click manual en
        # "Volver". Si en ese momento la ventana está en fullscreen (F/
        # Escape/botón lo pusieron ahí), hay que sacarla, si no la lista
        # de episodios queda mostrada dentro de una ventana fullscreen
        # sin bordes/barra de título/cursor, y sin ninguna forma de salir
        # de ese estado (los atajos de fullscreen viven en PlayerView,
        # que ya no está activo).
        if self.isFullScreen():
            self._toggle_fullscreen()
        self.player_view.stop_playback()
        self.stack.setCurrentWidget(self._player_return_view)
        if self._player_return_view is self.episode_view:
            self.episode_view.reload()
        elif self._player_return_view is self.gallery_view:
            # Volver directo a la galería pasa con películas — el
            # progreso recién guardado tiene que reflejarse en el
            # estante "Continuar viendo".
            self.gallery_view.refresh_continue_watching()

    def _on_storage_offline(self, offline, label):
        if offline:
            self.offline_banner.setText(
                f"No se pudo guardar progreso/preferencias en {label} — revisá la conexión."
            )
        self._offline_banner_pending = offline
        # En fullscreen el banner se mantiene oculto pase lo que pase (ver
        # _toggle_fullscreen) — el estado real queda guardado en
        # _offline_banner_pending y se reconcilia recién al salir.
        if not self.isFullScreen():
            self.offline_banner.setVisible(offline)

    def _show_gallery(self):
        # Ya no hace falta re-escanear acá: GalleryView cachea el último
        # scan por biblioteca, y un rename en la vista de episodios
        # parchea esa caché directo (ver episode_view.series_renamed /
        # GalleryView.update_series_name) sin pegarle a SMB de nuevo.
        self.stack.setCurrentWidget(self.gallery_view)
        # El progreso de los episodios vistos en esta sesión (reproductor
        # → volver a la lista de episodios → volver a la galería) tiene
        # que reflejarse en el estante "Continuar viendo".
        self.gallery_view.refresh_continue_watching()

    def _toggle_fullscreen(self):
        # Historia de este método — varias rondas de manipulación manual
        # de bits de windowState() (ver CONTEXT.md, Paso 14/15, Rondas
        # 3-10) intentando evitar el flicker de "frame restaurada" al
        # entrar/salir de fullscreen. Cada intento que evitaba el flicker
        # generaba un efecto secundario nuevo y más raro por el lado de
        # KWin: primero el panel de Plasma tapado, después los controles
        # de ventana (min/max/cerrar, integrados en el panel en este
        # setup) directamente desaparecidos — un estado de ventana "a
        # medias" (ni realmente Normal ni realmente Maximized según KWin)
        # que el widget de título de Plasma no reconocía. Decisión
        # explícita del usuario tras esa cascada de síntomas: preferir
        # los métodos ESTÁNDAR de Qt (los que usa cualquier app Qt normal,
        # con soporte real del ecosistema/KWin) y aceptar el flicker
        # cosmético breve, antes de seguir persiguiendo edge cases raros
        # de WM con manipulación de bits a mano.
        if self.isFullScreen():
            self.player_view.exit_fullscreen()
            # Recién ahora se reconcilia con el estado real (ver
            # _on_storage_offline) — mientras estuvo en fullscreen, un
            # cambio de estado se guardó en _offline_banner_pending sin
            # tocar la UI, para no tapar el video (Paso 9).
            self.offline_banner.setVisible(self._offline_banner_pending)
            if self._was_maximized_before_fullscreen:
                self.showMaximized()
            else:
                self.showNormal()
            # Reposiciona al área disponible (sin el panel de Plasma).
            # Antes esto se ignoraba silenciosamente cuando el flujo venía
            # de _player_back, porque PlayerView imponía un mínimo del
            # alto de pantalla completa — ver el comentario largo en
            # PlayerView._update_player_card_geometry (setMaximum* vs
            # setFixed*), que era la causa real de todo el bug del panel
            # tapado.
            screen = self.screen() or QApplication.primaryScreen()
            if screen:
                self.setGeometry(screen.availableGeometry())
        else:
            # IMPORTANTE: primero se le sacan las restricciones de tamaño
            # al reproductor (enter_fullscreen) y RECIÉN DESPUÉS se cambia
            # el estado de la ventana — si el orden fuera al revés, la
            # ventana ya llegaría a su tamaño de fullscreen mientras
            # PlayerView todavía tiene el ancho/alto fijado en modo
            # ventana, y el primer resize con las dimensiones de
            # fullscreen calcularía mal el tamaño (video angosto en el
            # medio de la pantalla en vez de a pantalla completa).
            self._was_maximized_before_fullscreen = self.isMaximized()
            self.offline_banner.setVisible(False)
            self.player_view.enter_fullscreen()
            self.showFullScreen()


def _wait_for_background_work(window):
    """Antes de os.execv (ver más abajo): le da a lo que pueda haber
    quedado escribiendo en background una chance real de terminar. La
    única garantía que la app ya tenía (aboutToQuit -> _save_progress_and_wait,
    ver MainWindow más abajo) solo cubre el progreso de la biblioteca que
    se está reproduciendo AHORA — acá se cubre lo que eso deja afuera:
    otras bibliotecas centralizadas con un flush en vuelo, y un rename de
    serie/temporada/película en curso. Deliberadamente NO se espera
    ScanWorker/ScanEpisodesWorker/ProgressRefreshWorker: son de solo
    lectura, abandonarlos a mitad de camino no pierde nada, el próximo
    arranque vuelve a escanear solo.

    `window` puede ser el MainWindow de escritorio o el placeholder de TV
    (tv_main.TVMainWindow, que todavía no tiene ninguno de estos atributos)
    — de ahí el getattr con default None en todos lados."""
    player_view = getattr(window, "player_view", None)
    if player_view is not None and player_view.media_player.get_state() == vlc.State.Playing:
        player_view.media_player.stop()

    episode_view = getattr(window, "episode_view", None)
    save_worker = getattr(episode_view, "_save_worker", None)
    if save_worker is not None and save_worker.isRunning():
        save_worker.wait(3000)

    rename_worker = getattr(player_view, "_rename_worker", None)
    if rename_worker is not None and rename_worker.isRunning():
        rename_worker.wait(3000)

    progress_store.wait_all_pending()
    track_prefs.wait_all_pending()


def main():
    mode = app_settings.get_ui_mode()
    if mode == "tv":
        # Bug real encontrado en vivo: bajo una sesión Wayland, Qt6 elige
        # solo el plugin nativo "wayland" (WAYLAND_DISPLAY presente), y esa
        # combinación puntual (KWin 6.3.6 + Qt 6.8.2 en Debian trixie, al
        # menos) corrompía visualmente el texto de los botones en la
        # ventana fullscreen del modo TV (glitch de repintado, reproducido
        # SIN ningún cambio de código propio — confirmado forzando este
        # mismo plugin a mano). set_xwindow() (embebido de libVLC, ver
        # player_engine.py) también depende de X11/XWayland real (mismo
        # motivo que el manifest de Flatpak NUNCA usa --socket=fallback-x11,
        # ver flatpak/io.github.antarest47.Lunyx.yml) — forzar "xcb" corre
        # la ventana fullscreen vía XWayland en vez del path nativo, lo que
        # además esquiva el bug de arriba. setdefault() (no asignación
        # directa) para no pisar un QT_QPA_PLATFORM explícito si alguien lo
        # seteó a mano por otro motivo. Solo en modo TV — el modo desktop
        # (nunca fullscreen salvo que el usuario lo pida) no mostró este bug.
        os.environ.setdefault("QT_QPA_PLATFORM", "xcb")

    app = QApplication(sys.argv)
    # Sin esto, el gestor de tareas de Plasma no reconoce la ventana real
    # como la misma app que el .desktop exportado del Flatpak (a
    # diferencia de GTK, Qt no lo hace solo) — bug real reportado en vivo:
    # aparecían DOS íconos en la barra de tareas al abrir la app, y anclar
    # el ícono real desde su propio menú contextual quedaba deshabilitado.
    app.setDesktopFileName("io.github.antarest47.Lunyx")
    app.setStyleSheet(theme.STYLESHEET)
    app.setWindowIcon(QIcon(str(Path(__file__).parent / "logo.png")))

    if mode == "tv":
        window = tv_main.TVMainWindow()
        app.aboutToQuit.connect(window.player_view._save_progress_and_wait)
        window.showFullScreen()
    else:
        window = MainWindow()
        app.aboutToQuit.connect(window.player_view._save_progress_and_wait)
        window.show()

    exit_code = app.exec()

    new_mode = app_restart.pending_restart_mode()
    if new_mode is not None:
        _wait_for_background_work(window)
        os.execv(sys.executable, [sys.executable] + sys.argv)

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
