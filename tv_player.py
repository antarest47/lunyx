"""
Reproductor de TV — Paso 4 de la iniciativa "modo TV" (ver CONTEXT.md).
Clase completamente separada de main.PlayerView, mismo criterio que el
resto de las vistas TV (nunca comparten widgets con desktop). Reusa
player_engine.PlayerEngine (motor libVLC compartido: instancia, set_xwindow,
threading de eventos) y las funciones puras de player_engine
(resolve_previous_action, resolve_auto_advance, build_track_picker_options)
— la parte con más bugs sutiles ya resueltos en desktop, sin duplicarla.
También reusa tv_episodes.TVValuePicker tal cual (widget chico y genérico,
sin acoplar a ninguna vista puntual).

A diferencia de PlayerView (que alterna modo ventana ↔ fullscreen),
TVPlayerView vive SIEMPRE dentro de TVMainWindow, que ya arranca en
showFullScreen() — no hay "modo ventana" acá, así que no hace falta nada
del cálculo de aspect-ratio/geometría de PlayerView. El video ocupa toda
la vista desde el arranque, con una barra de controles flotante (hija de
video_frame, WA_NativeWindow, mismo truco que PlayerView.enter_fullscreen
del Paso 9) que se autooculta con inactividad.

Sin renombrar película ni panel de corrección — mismo alcance acordado
para la lista de episodios (Paso 3). SÍ incluye cambiar audio/subtítulos
EN VIVO durante la reproducción, con Enter en el picker para guardarlo
como preferencia de la serie (equivalente TV del combo + botón "Usar
para toda la serie" de escritorio, condensado en un solo control)."""

import time

import vlc
from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

import player_engine
import player_icons
import progress_store
import theme
import track_prefs
import tv_input
from tv_episodes import TVValuePicker

HIDE_CONTROLS_DELAY_MS = 3000
OVERLAY_MARGIN = 24
# Ventana de gracia después de que el overlay se autooculta durante la cual
# Escape no sale del video — evita que un Escape que llega una fracción de
# segundo tarde (pensado para ocultar el overlay) termine saliendo por error.
ESCAPE_EXIT_GRACE_MS = 2000
# Cuánto queda visible el flash del indicador de esquina (TVCornerIndicator:
# play/pausa/seek) antes de autoocultarse — siempre el mismo tiempo fijo,
# independiente de qué ícono se esté mostrando.
CORNER_INDICATOR_MS = 700
# Ventana de gracia después de abrir un video (play_url) durante la cual
# mouse_moved NO muestra el overlay completo — video_frame puede cambiar
# de tamaño/posición al entrar a la vista (el layout recién se asienta),
# y Qt puede generar un mouseMoveEvent sintético bajo un cursor que en
# los hechos no se movió (posición local relativa al widget cambia igual
# si el widget se mueve/redimensiona debajo). Sin esto, ese evento
# fantasma reabría el overlay completo justo al abrir — bug real
# reportado en vivo, no reproducible en este entorno sin mouse/pantalla
# real (ver CORNER_INDICATOR_MS arriba, mismo criterio de "tiempo fijo
# simple" que el resto de estos flashes).
OPEN_SUPPRESS_MOUSE_MOVE_MS = 500


class TVVideoFrame(QFrame):
    """Lienzo en blanco: le pasamos su window id a libVLC para que dibuje
    el video ahí adentro, por fuera del sistema de dibujado de Qt (ver
    Paso 1 en CONTEXT.md). Navegación principal por flechas/Enter, no
    puntero — pero sí reacciona a mouse_moved para reaparecer el overlay
    (pedido explícito del usuario, mismo criterio que main.VideoFrame en
    fullscreen: en la práctica el modo TV también se usa desde una PC de
    escritorio con mouse a mano, no solo control remoto)."""

    resized = Signal()
    mouse_moved = Signal()

    def __init__(self):
        super().__init__()
        self.setStyleSheet("background-color: black;")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)

    def mouseMoveEvent(self, event):
        self.mouse_moved.emit()
        super().mouseMoveEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.resized.emit()


class TVProgressBar(QWidget):
    """Barra de progreso puramente visual (no arrastrable con flechas —
    el seek real es Izquierda/Derecha sobre el video, ±10s, igual que
    rebobinar/adelantar en un control remoto real)."""

    def __init__(self):
        super().__init__()
        self.setFixedHeight(6)
        # WA_StyledBackground: sin esto, un QWidget liso IGNORA el
        # background-color de su propio stylesheet (solo lo pintan
        # widgets que lo dibujan nativamente, como QFrame/QPushButton) —
        # bug real reportado en vivo: no se veía ningún fondo, solo la
        # línea ámbar creciendo, sin referencia del largo total.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"background-color: {theme.TEXT_DIM}; border-radius: 3px;")
        self._fraction = 0.0
        self._fill = QWidget(self)
        self._fill.setStyleSheet(f"background-color: {theme.ACCENT}; border-radius: 3px;")

    def set_fraction(self, fraction):
        self._fraction = max(0.0, min(1.0, fraction))
        self._update_fill_geometry()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_fill_geometry()

    def _update_fill_geometry(self):
        self._fill.setGeometry(0, 0, round(self.width() * self._fraction), self.height())


class TVIconButton(QPushButton):
    """Mismos íconos dibujados a mano que main.IconButton (ver
    player_icons.py, extraído ahí para que sean IDÉNTICOS entre las dos
    vistas — pedido explícito del usuario), pero con foco/tamaño/estilo
    propios de TV: más grande (a distancia de sillón hay que verlo bien)
    y con borde de foco ámbar (main.IconButton no lo necesita, sus
    botones nunca toman foco — ver PlayerView.__init__)."""

    SIZE = 56

    def __init__(self, icon_name, tooltip=""):
        super().__init__()
        self._icon_name = icon_name
        self.setObjectName("tvPlayerIconButton")
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        # QPushButton solo reacciona a Enter/Return vía su propio
        # keyPressEvent si autoDefault (o default) está activado — por
        # default eso es True adentro de un QDialog, pero False en un
        # QWidget suelto como esta vista (ver misma nota en
        # tv_gallery.manage_button). Espacio ya funcionaba sin esto
        # (QAbstractButton lo maneja siempre); Enter necesitaba este flag.
        self.setAutoDefault(True)
        if tooltip:
            self.setToolTip(tooltip)

    def set_icon_name(self, name):
        if name != self._icon_name:
            self._icon_name = name
            self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = QColor(theme.ACCENT if self.hasFocus() else theme.TEXT)
        painter.setBrush(color)
        painter.setPen(Qt.PenStyle.NoPen)
        cx, cy = self.width() / 2, self.height() / 2
        s = min(self.width(), self.height()) * 0.16
        player_icons.paint_icon(painter, self._icon_name, cx, cy, s, color)
        painter.end()


class TVCornerIndicator(QWidget):
    """Flash breve en la esquina superior izquierda del video con un
    ícono de player_icons (play/pausa al togglear, doble triángulo al
    saltar ±10s) — pedido explícito del usuario para que quede claro que
    la acción surtió efecto incluso cuando el overlay de controles se
    queda oculto (ver TVPlayerView._on_play_pause_toggled/
    _on_seek_key_pressed). Cuadro negro con el ícono en blanco, mismos
    símbolos que los botones reales (ver TVIconButton). Hija de
    video_frame con WA_NativeWindow, mismo truco que control_bar_widget
    para componer arriba de lo que dibuja libVLC ahí adentro.

    Se probó WA_TranslucentBackground para un fondo semitransparente en
    vez de negro sólido — reportado en vivo que dejó de renderizar el
    ícono (solo un parpadeo del cuadro) en la máquina real del usuario,
    probablemente por falta de compositor activo. Revertido: fondo negro
    opaco a secas, sin intentar transparencia real."""

    SIZE = 64

    def __init__(self, parent):
        super().__init__(parent)
        self.setFixedSize(self.SIZE, self.SIZE)
        # WA_NativeWindow: ventana X11 propia, hija de video_frame — así
        # compone arriba de lo que dibuja libVLC ahí adentro, mismo truco
        # que control_bar_widget.
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._icon_name = "play"
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide)
        self.hide()

    def flash(self, icon_name):
        self._icon_name = icon_name
        self.update()
        self.show()
        self.raise_()
        self._hide_timer.start(CORNER_INDICATOR_MS)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0))
        painter.drawRect(self.rect())

        color = QColor(255, 255, 255)
        painter.setBrush(color)
        cx, cy = self.width() / 2, self.height() / 2
        s = self.width() * 0.16
        player_icons.paint_icon(painter, self._icon_name, cx, cy, s, color)


class TVPlayerView(QWidget):
    back_requested = Signal()
    episode_change_requested = Signal(tuple, str)

    def __init__(self):
        super().__init__()
        self._engine = player_engine.PlayerEngine()
        self.media_player = self._engine.media_player

        self.playlist = []
        self.playlist_index = -1
        self.series_key = None

        self.video_frame = TVVideoFrame()
        setattr(self.video_frame, tv_input.TV_OWNS_HORIZONTAL_NAV_ATTR, True)
        self.video_frame._tv_focus_override = {"up": None, "down": lambda: self.play_pause_button}
        self.video_frame.resized.connect(self._reposition_overlay)
        self.video_frame.mouse_moved.connect(self._on_video_mouse_moved)

        # --- Barra flotante (título + fila de reproducción + fila de pistas) ---
        self.title_label = QLabel("")
        self.title_label.setStyleSheet("font-size: 22px; font-weight: 700; color: #f2f3f5;")

        self.back_button = TVIconButton("back", "Volver")
        self.prev_button = TVIconButton("prev", "Episodio anterior")
        self.play_pause_button = TVIconButton("play", "Reproducir/Pausar")
        self.next_button = TVIconButton("next", "Episodio siguiente")
        self.mute_button = TVIconButton("volume", "Silenciar")
        self.back_button.clicked.connect(self.back_requested.emit)
        self.prev_button.clicked.connect(self._play_previous)
        self.play_pause_button.clicked.connect(self._on_play_pause_toggled)
        self.next_button.clicked.connect(self._play_next)
        self.mute_button.clicked.connect(self._toggle_mute)

        self.elapsed_label = QLabel("0:00")
        self.elapsed_label.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 14px;")
        self.duration_label = QLabel("0:00")
        self.duration_label.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 14px;")
        self.progress_bar = TVProgressBar()

        playback_row = QHBoxLayout()
        playback_row.setSpacing(12)
        playback_row.addWidget(self.back_button)
        playback_row.addWidget(self.prev_button)
        playback_row.addWidget(self.play_pause_button)
        playback_row.addWidget(self.next_button)
        playback_row.addWidget(self.elapsed_label)
        playback_row.addWidget(self.progress_bar, stretch=1)
        playback_row.addWidget(self.duration_label)
        playback_row.addWidget(self.mute_button)

        self.audio_picker = TVValuePicker("Audio")
        self.subtitle_picker = TVValuePicker("Subtítulos")
        self.audio_picker.value_changed.connect(self._on_audio_selected)
        self.subtitle_picker.value_changed.connect(self._on_subtitle_selected)
        self.audio_picker.activated.connect(self._save_audio_pref)
        self.subtitle_picker.activated.connect(self._save_subtitle_pref)

        # Columna, no fila: la navegación entre estos dos pickers usa
        # Arriba/Abajo (ver _rewire_control_row_navigation) — Izquierda/
        # Derecha están tomadas por el propio picker para ciclar valores
        # (TV_OWNS_HORIZONTAL_NAV_ATTR), así que no hay forma de moverse
        # ENTRE pickers con esas teclas (bug real reportado en vivo:
        # "solo pude cambiar el audio, no puedo navegar hasta subs").
        tracks_col = QVBoxLayout()
        tracks_col.setSpacing(8)
        tracks_col.addWidget(self.audio_picker)
        tracks_col.addWidget(self.subtitle_picker)

        overlay_layout = QVBoxLayout()
        overlay_layout.setContentsMargins(20, 14, 20, 14)
        overlay_layout.setSpacing(10)
        overlay_layout.addWidget(self.title_label)
        overlay_layout.addLayout(playback_row)
        overlay_layout.addLayout(tracks_col)

        self.control_bar_widget = QWidget(self.video_frame)
        self.control_bar_widget.setObjectName("tvPlayerOverlay")
        self.control_bar_widget.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        # WA_NativeWindow: ventana X11 propia, hija de video_frame — así
        # compone ARRIBA de lo que dibuja libVLC ahí adentro (por fuera
        # del sistema de pintado de Qt, set_xwindow), mismo truco que
        # PlayerView.enter_fullscreen (Paso 9 en CONTEXT.md).
        self.control_bar_widget.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.control_bar_widget.setLayout(overlay_layout)

        self.corner_indicator = TVCornerIndicator(self.video_frame)

        # QWidget#tvValuePicker/:focus repetido acá (mismas reglas que
        # tv_episodes.py): un setStyleSheet() aplica solo al widget donde
        # se llama y sus descendientes, nunca globalmente — el picker
        # vive DENTRO de esta vista, no de TVEpisodeListView, así que sin
        # esto quedaba sin ningún borde de foco acá (bug real reportado
        # en vivo: "no queda resaltado qué opción estoy seleccionando").
        self.setStyleSheet(f"""
            QWidget#tvPlayerOverlay {{
                background-color: #000000;
                border-radius: 0px;
            }}
            QPushButton#tvPlayerIconButton {{
                background-color: {theme.SURFACE};
                border: 2px solid transparent;
                border-radius: {TVIconButton.SIZE // 2}px;
            }}
            QPushButton#tvPlayerIconButton:focus {{
                border: 2px solid {theme.ACCENT};
                background-color: rgba({theme.ACCENT_RGB}, 0.15);
            }}
            QWidget#tvValuePicker {{
                background-color: {theme.SURFACE};
                border-radius: 8px;
                border: 2px solid transparent;
            }}
            QWidget#tvValuePicker:focus {{
                border: 2px solid {theme.ACCENT};
            }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.video_frame)

        self._hide_controls_timer = QTimer(self)
        self._hide_controls_timer.setInterval(HIDE_CONTROLS_DELAY_MS)
        self._hide_controls_timer.setSingleShot(True)
        self._hide_controls_timer.timeout.connect(self._hide_controls)
        self._cursor_hidden = False
        self._auto_hiding = False
        self._controls_hidden_at = None

        self._engine.media_playing.connect(self._on_media_playing)
        self._engine.media_end_reached.connect(self._on_media_end)
        self._engine.media_failed.connect(self._on_media_failed)
        self._suppress_end_event = False

        self._progress_timer = QTimer(self)
        self._progress_timer.setInterval(player_engine.PROGRESS_SAVE_INTERVAL_MS)
        self._progress_timer.timeout.connect(self._save_progress)

        self._ui_timer = QTimer(self)
        self._ui_timer.setInterval(player_engine.UI_REFRESH_INTERVAL_MS)
        self._ui_timer.timeout.connect(self._update_ui_position)

        self._current_library = None
        self._current_library_id = None
        self._current_subpath = None
        self._resume_position_ms = None
        self._base_title = ""
        self._play_started_at = 0.0
        self._open_suppress_mouse_move_until = 0.0

        self._rewire_control_row_navigation()
        tv_input.install_spatial_navigation(self)
        tv_input.install_escape_back(self, self._handle_escape)
        QApplication.instance().focusChanged.connect(self._on_focus_changed)

        self.control_bar_widget.hide()  # recién se muestra al arrancar a reproducir (play_url)
        self._reposition_overlay()

    # ---- navegación ----

    def _rewire_control_row_navigation(self):
        """Misma técnica que TVGalleryView._rewire_top_row_navigation:
        cada fila/columna queda encadenada de punta a punta, bloqueada en
        los extremos, y las transiciones entre filas/el video van a un
        destino fijo — nunca a lo que la geometría cruda considere "más
        cerca" (ver tv_input.TV_FOCUS_OVERRIDE_ATTR).

        Los pickers de audio/subtítulos se navegan entre sí con Arriba/
        Abajo, NO Izquierda/Derecha — un TVValuePicker "posee"
        Izquierda/Derecha para ciclar su propio valor
        (TV_OWNS_HORIZONTAL_NAV_ATTR), lo que hace que el filtro global
        nunca llegue a evaluar overrides de esas direcciones en él (bug
        real encontrado en vivo: no había forma de pasar de Audio a
        Subtítulos). Por eso tracks_col es una COLUMNA (ver __init__),
        no una fila."""
        playback_row = [self.back_button, self.prev_button, self.play_pause_button,
                         self.next_button, self.mute_button]
        for i, widget in enumerate(playback_row):
            widget._tv_focus_override = {
                "left": playback_row[i - 1] if i > 0 else None,
                "right": playback_row[i + 1] if i + 1 < len(playback_row) else None,
                "up": self.video_frame,
                "down": lambda: self.audio_picker,
            }
        self.audio_picker._tv_focus_override = {
            "up": lambda: self.play_pause_button,
            "down": lambda: self.subtitle_picker,
        }
        self.subtitle_picker._tv_focus_override = {
            "up": lambda: self.audio_picker,
            "down": None,
        }

    def _on_focus_changed(self, old, now):
        # _hide_controls() abajo mueve el foco de vuelta a video_frame
        # cuando oculta la barra — sin este guard, ESE cambio de foco
        # disparaba este mismo handler, que volvía a mostrar la barra y
        # reiniciaba el timer de 3s (bug real reportado en vivo: la
        # interfaz tardaba el doble de lo esperado en desaparecer, con el
        # borde de foco ámbar desapareciendo primero y la barra recién
        # después). self._auto_hiding marca ese movimiento de foco
        # puntual como "no es actividad real", así no reabre lo que
        # recién se cerró.
        if self._auto_hiding:
            return
        if now is not None and self.isAncestorOf(now):
            self._restart_hide_timer()

    # ---- reproducción ----

    def prepare_for_open(self):
        """Llamar ANTES de stack.setCurrentWidget(self.player_view) en
        tv_main.py — confirmado en vivo con un rastreo real
        (traceback.print_stack() en _show_controls()) que ESE
        setCurrentWidget por sí solo ya dispara _on_focus_changed()
        (Qt mueve/restaura el foco al volver a mostrar la vista, antes
        de que play_url() llegue siquiera a correr), reabriendo el
        overlay completo. Guardar self._auto_hiding acá (en vez de
        recién adentro de play_url(), como el primer intento de este
        fix) cubre también ese momento anterior. play_url() cierra la
        ventana de supresión al final, después de todo lo que hace con
        foco."""
        self._auto_hiding = True

    def play_url(self, url, title, library_id, subpath, extra_options=(),
                 playlist=None, playlist_index=-1, series_key=None, library=None):
        self._suppress_end_event = False
        self._play_started_at = time.monotonic()
        self._open_suppress_mouse_move_until = time.monotonic() + OPEN_SUPPRESS_MOUSE_MOVE_MS / 1000
        self._base_title = title
        self.title_label.setText(title)

        self._current_library = library
        self._current_library_id = library_id
        self._current_subpath = subpath
        self._resume_position_ms = progress_store.get_progress_ms(library, subpath) if library else None

        self.playlist = playlist or []
        self.playlist_index = playlist_index
        self.series_key = series_key
        self._update_nav_buttons()

        self._engine.play_url(url, extra_options, int(self.video_frame.winId()))
        self._progress_timer.start()
        self._ui_timer.start()
        # Por si play_url() se llama sin pasar por prepare_for_open()
        # antes (defensivo) — ver esa función para el porqué.
        self._auto_hiding = True
        self.video_frame.setFocus()
        self._auto_hiding = False
        self._flash_open_title_marquee()
        self.corner_indicator.flash("play")
        self._hide_cursor()  # sin overlay visible que lo oculte por su cuenta (ver Paso 45)

    def _flash_open_title_marquee(self):
        """Al abrir un video (play_url) ya NO se muestra el overlay
        completo por 3s (antes: _restart_hide_timer() acá mismo) — eso
        tapaba subtítulos durante ese tiempo (mismo problema de fondo que
        Paso 43, pero en el momento de abrir en vez de al aparecer el
        overlay por inactividad). En cambio, el título (+ "retomando
        desde X:XX" si corresponde) se muestra como texto de VLC (filtro
        `marq`) directo sobre el video, junto al corner_indicator.

        Elegido sobre un widget Qt a propósito: TVCornerIndicator
        necesitó terminar con una caja opaca detrás porque
        WA_TranslucentBackground resultó poco confiable en la máquina
        real del usuario (ver esa clase) — el marquee lo dibuja libVLC
        mismo sobre el frame, con transparencia real garantizada, sin
        depender de ningún compositor. Contra aceptada a propósito
        (pedido explícito del usuario, "no es info tan importante"): el
        módulo `marq` no tiene NINGUNA opción de contorno/borde
        (confirmado corriendo `vlc -p marq --advanced --help-verbose` —
        solo texto/posición/X/Y/opacidad/color/tamaño/timeout/refresh),
        así que el texto puede mezclarse con fondos claros del video."""
        text = self._base_title
        if self._resume_position_ms:
            text = f"{text} (retomando desde {player_engine.format_time(self._resume_position_ms)})"
        x = OVERLAY_MARGIN + TVCornerIndicator.SIZE + 12
        self.media_player.video_set_marquee_string(vlc.VideoMarqueeOption.Text, text)
        self.media_player.video_set_marquee_int(vlc.VideoMarqueeOption.Position, 5)  # arriba-izquierda
        self.media_player.video_set_marquee_int(vlc.VideoMarqueeOption.X, x)
        self.media_player.video_set_marquee_int(vlc.VideoMarqueeOption.Y, OVERLAY_MARGIN + 16)
        self.media_player.video_set_marquee_int(vlc.VideoMarqueeOption.Size, 28)
        self.media_player.video_set_marquee_int(vlc.VideoMarqueeOption.Color, 0xFFFFFF)
        self.media_player.video_set_marquee_int(vlc.VideoMarqueeOption.Opacity, 230)
        self.media_player.video_set_marquee_int(vlc.VideoMarqueeOption.Timeout, HIDE_CONTROLS_DELAY_MS)
        self.media_player.video_set_marquee_int(vlc.VideoMarqueeOption.Enable, 1)

    def _update_nav_buttons(self):
        is_series = self.series_key is not None
        self.prev_button.setVisible(is_series)
        self.next_button.setVisible(is_series)
        if is_series:
            self.prev_button.setEnabled(True)
            self.next_button.setEnabled(0 <= self.playlist_index < len(self.playlist) - 1)

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

    def _toggle_play_pause(self):
        """Devuelve True si quedó pausado, False si quedó reproduciendo —
        lo usa _on_play_pause_toggled para decidir qué hacer con el
        overlay/indicador sin volver a consultar get_state() (que ya lo
        determinó acá, evitar una segunda consulta separada por si el
        estado cambia justo entre medio)."""
        if self.media_player.get_state() == vlc.State.Playing:
            self.media_player.pause()
            self._save_progress()
            # Async, sin esperar — ver main.py PlayerView._toggle_play_pause,
            # mismo criterio (pausar tiene que sentirse instantáneo).
            if self._current_library is not None:
                progress_store.checkpoint(self._current_library)
            return True
        self.media_player.play()
        return False

    def _on_play_pause_toggled(self):
        """Wrapper de _toggle_play_pause() para las dos formas de
        togglear (Espacio/Enter sobre el video, o clickear el botón real)
        — pedido explícito del usuario: pausar muestra el overlay al
        toque (ya pasaba antes), pero reanudar ahora lo oculta al toque
        en vez de mostrarlo/reiniciar el autoocultado de 3s. El flash de
        corner_indicator confirma el toggle en los dos casos, para que
        se note incluso cuando el overlay se queda oculto."""
        paused = self._toggle_play_pause()
        self.corner_indicator.flash("pause" if paused else "play")
        if paused:
            self._restart_hide_timer()
        else:
            self._hide_controls_timer.stop()
            self._hide_controls()

    def _toggle_mute(self):
        muted = self.media_player.audio_get_mute()
        self.media_player.audio_set_mute(not muted)
        self.mute_button.set_icon_name("volume_muted" if not muted else "volume")

    # ---- overlay auto-ocultable (mismo mecanismo que PlayerView.enter_fullscreen, Paso 9) ----

    def keyPressEvent(self, event):
        key = event.key()
        if self.video_frame.hasFocus():
            if key == Qt.Key.Key_Space or key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Select):
                self._on_play_pause_toggled()
                return
            if key == Qt.Key.Key_Right:
                self._skip(player_engine.SKIP_MS)
                self._on_seek_key_pressed(forward=True)
                return
            if key == Qt.Key.Key_Left:
                self._skip(-player_engine.SKIP_MS)
                self._on_seek_key_pressed(forward=False)
                return
        super().keyPressEvent(event)

    def _on_seek_key_pressed(self, forward):
        """Pedido explícito del usuario: Izquierda/Derecha ya NO muestran
        el overlay completo por sí solos (antes sí, vía
        _restart_hide_timer() acá) — solo el flash de corner_indicator
        con el doble triángulo correspondiente. PERO si el overlay YA
        está en pantalla (pausado, o se navegó adentro con Abajo) tiene
        que seguir comportándose como antes: quedarse visible y
        reiniciar el autoocultado de 3s, sin el flash (el progreso ya se
        ve reflejado en la barra del overlay, un segundo indicador ahí
        encima sería ruido)."""
        if self.control_bar_widget.isVisible():
            self._restart_hide_timer()
        else:
            self.corner_indicator.flash("seek_forward" if forward else "seek_back")

    def _on_video_mouse_moved(self):
        if time.monotonic() < self._open_suppress_mouse_move_until:
            return
        self._restart_hide_timer()

    def _restart_hide_timer(self):
        self._show_controls()
        self._hide_controls_timer.start()

    def _handle_escape(self):
        """Escape con el overlay visible lo oculta en el acto; Escape sin
        overlay recién sale del video después de ESCAPE_EXIT_GRACE_MS desde
        que se ocultó (ver ESCAPE_EXIT_GRACE_MS más arriba)."""
        if self.control_bar_widget.isVisible():
            self._hide_controls_timer.stop()
            self._hide_controls()
            return
        if self._controls_hidden_at is not None:
            elapsed_ms = (time.monotonic() - self._controls_hidden_at) * 1000
            if elapsed_ms < ESCAPE_EXIT_GRACE_MS:
                return
        self.back_requested.emit()

    def _show_controls(self):
        self.control_bar_widget.show()
        self.control_bar_widget.raise_()
        if self._cursor_hidden:
            QApplication.restoreOverrideCursor()
            self._cursor_hidden = False

    def _hide_controls(self):
        # Idempotente a propósito: _on_play_pause_toggled puede llamar acá
        # con el overlay YA oculto (reanudar mientras estaba oculto tras
        # el autoocultado de 3s) — sin este guard, QApplication.setOverrideCursor()
        # más abajo apilaría un segundo BlankCursor sin que hubiera un
        # restoreOverrideCursor() de más para sacarlo, dejando el cursor
        # invisible para siempre en la próxima vez que se muestre.
        if not self.control_bar_widget.isVisible():
            return
        # _auto_hiding tiene que estar en True ANTES de hide() — Qt
        # reasigna el foco automáticamente en el momento en que se oculta
        # un ancestro del widget enfocado (no recién en el
        # video_frame.setFocus() explícito de más abajo), y esa
        # reasignación automática también dispara focusChanged. Sin
        # cubrir ese primer disparo, el bug de "doble timeout" seguía
        # pasando: el guard llegaba tarde, después de que Qt ya hubiera
        # reabierto la barra solo.
        self._auto_hiding = True
        self.control_bar_widget.hide()
        self.video_frame.setFocus()
        self._auto_hiding = False

        self._hide_cursor()
        self._controls_hidden_at = time.monotonic()

    def _hide_cursor(self):
        # Extraído de _hide_controls() para poder ocultar el cursor
        # también al abrir un video (play_url), donde el overlay nunca
        # llega a mostrarse (ver Paso 45) — _hide_controls() tiene su
        # propio guard de "overlay visible" que bloquearía este caso si
        # se llamara a _hide_controls() directo.
        if self._cursor_hidden:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.BlankCursor)
        self._cursor_hidden = True

    def _reposition_overlay(self):
        height = self.control_bar_widget.sizeHint().height()
        self.control_bar_widget.setGeometry(
            0, self.video_frame.height() - height, self.video_frame.width(), height,
        )
        self.corner_indicator.move(OVERLAY_MARGIN, OVERLAY_MARGIN)

    # ---- progreso ----

    def stop_playback(self):
        self._suppress_end_event = True
        self._hide_controls_timer.stop()
        if self._cursor_hidden:
            QApplication.restoreOverrideCursor()
            self._cursor_hidden = False
        self._progress_timer.stop()
        self._ui_timer.stop()
        self._save_progress()
        # Bloqueante CON tope — ver main.py PlayerView.stop_playback,
        # mismo criterio ("Volver" espera a que el último guardado llegue
        # al target centralizado antes de dejar avanzar la navegación).
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
        """Usado por app.aboutToQuit (ver main.py) — mismo criterio que
        PlayerView._save_progress_and_wait."""
        self._save_progress()
        if self._current_library is not None:
            progress_store.checkpoint(self._current_library, blocking=True)

    def _update_ui_position(self):
        length = self.media_player.get_length()
        position = self.media_player.get_time()
        if length and length > 0:
            self.duration_label.setText(player_engine.format_time(length))
            self.progress_bar.set_fraction(position / length if position else 0)
        self.elapsed_label.setText(player_engine.format_time(position))
        is_playing = self.media_player.get_state() == vlc.State.Playing
        self.play_pause_button.set_icon_name("pause" if is_playing else "play")

    # ---- eventos de libVLC (ya en el thread de Qt, ver PlayerEngine) ----

    def _on_media_end(self):
        if self._suppress_end_event:
            return
        result = player_engine.resolve_auto_advance(
            self.media_player, self.playlist, self.playlist_index, self._play_started_at
        )
        if result is None:
            return
        action, subpath, title = result
        if action == "advance":
            self.episode_change_requested.emit(subpath, title)
        else:
            self.back_requested.emit()

    def _on_media_failed(self, message):
        # Sin QMessageBox acá a propósito — ninguna vista TV usa diálogos
        # modales (ver CONTEXT.md, navegación por control remoto), así que
        # solo se vuelve atrás en silencio. _save_progress no-opea con
        # duration_ms<=0 (ver progress_store.save_progress), no hace falta
        # guardar nada acá.
        self._progress_timer.stop()
        self._ui_timer.stop()
        self.back_requested.emit()

    def _on_media_playing(self):
        # Ver player_engine.TRACKS_REFRESH_RETRY_DELAYS_MS — varios
        # intentos en vez de uno solo, la lista de pistas puede tardar más
        # de un intento en estar completa sobre una red más lenta.
        for delay_ms in player_engine.TRACKS_REFRESH_RETRY_DELAYS_MS:
            QTimer.singleShot(delay_ms, self._refresh_tracks)
        if self._resume_position_ms:
            self.media_player.set_time(self._resume_position_ms)
            # title_label ya NO se toca acá — el "(retomando desde X:XX)"
            # lo mostró el marquee de _flash_open_title_marquee() al
            # abrir, y title_label es el título del overlay COMPLETO
            # (solo visible mucho después, si el usuario navega adentro
            # con Abajo) — mostrarlo ahí ya sería información vieja.
            self._resume_position_ms = None

    def _refresh_tracks(self):
        audio_descriptions = self.media_player.audio_get_track_description()
        subtitle_descriptions = self.media_player.video_get_spu_description()
        current_audio = self.media_player.audio_get_track()
        current_subtitle = self.media_player.video_get_spu()

        audio_options, audio_index = player_engine.build_track_picker_options(audio_descriptions, current_audio)
        subtitle_options, subtitle_index = player_engine.build_track_picker_options(
            subtitle_descriptions, current_subtitle
        )

        if self.series_key and self._current_library is not None:
            prefs = track_prefs.get_prefs(self._current_library, self.series_key[1])
            audio_pref_id = track_prefs.find_matching_track(audio_descriptions, prefs.get("audio"))
            if audio_pref_id is not None:
                audio_index = next((i for i, (_l, tid) in enumerate(audio_options) if tid == audio_pref_id), audio_index)
            subtitle_pref_id = track_prefs.find_matching_track(subtitle_descriptions, prefs.get("subtitle"))
            if subtitle_pref_id is not None:
                subtitle_index = next(
                    (i for i, (_l, tid) in enumerate(subtitle_options) if tid == subtitle_pref_id), subtitle_index
                )

        self.audio_picker.set_options(audio_options, audio_index)
        self.subtitle_picker.set_options(subtitle_options, subtitle_index)
        # Aplica la selección resuelta (preferencia de serie si matcheó,
        # si no la que ya trae el archivo) — set_options no dispara
        # value_changed, así que hay que aplicarla a mano una vez.
        self._apply_track_selection(self.audio_picker, self.media_player.audio_set_track)
        self._apply_track_selection(self.subtitle_picker, self.media_player.video_set_spu)

    def _apply_track_selection(self, picker, setter):
        _label, track_id = picker.current()
        if track_id is not None:
            setter(track_id)

    def _on_audio_selected(self, _index):
        _label, track_id = self.audio_picker.current()
        if track_id is not None:
            self.media_player.audio_set_track(track_id)

    def _on_subtitle_selected(self, _index):
        _label, track_id = self.subtitle_picker.current()
        if track_id is not None:
            self.media_player.video_set_spu(track_id)

    def _save_audio_pref(self):
        if not self.series_key or self._current_library is None:
            return
        label, _value = self.audio_picker.current()
        track_prefs.set_audio_pref(self._current_library, self.series_key[1], label)

    def _save_subtitle_pref(self):
        if not self.series_key or self._current_library is None:
            return
        label, _value = self.subtitle_picker.current()
        track_prefs.set_subtitle_pref(self._current_library, self.series_key[1], label)
