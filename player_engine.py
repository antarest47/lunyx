"""
Motor de reproducción compartido entre el reproductor de escritorio
(main.PlayerView) y el de TV (tv_player.TVPlayerView) — la parte con más
bugs sutiles ya resueltos (instancia de libVLC, set_xwindow, threading de
eventos; ver Pasos 1/4/5/8/9 en CONTEXT.md), sin NINGÚN widget. Cada
vista arma su PROPIA barra de controles encima — nunca comparten esa
parte (decisión explícita del usuario, ver "Modo TV" en CONTEXT.md).
"""

import time

import vlc
from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtDBus import QDBusConnection, QDBusInterface

DEFAULT_VOLUME = 80
SKIP_MS = 10_000
RESTART_THRESHOLD_MS = 10_000
PROGRESS_SAVE_INTERVAL_MS = 5000
UI_REFRESH_INTERVAL_MS = 500
# Reintentos para aplicar la preferencia de pista de la serie apenas
# arranca Playing — las descripciones de pista no siempre están TODAS
# listas de entrada (dependen de que el demux termine de leer los headers
# de cada pista, y con audio dual/varios subtítulos, algo típico en
# anime, eso puede tardar más de un intento sobre una red más lenta). Un
# solo intento a los 300ms alcanzaba en la máquina de desarrollo, pero no
# en una red más lenta (ver CONTEXT.md: bug real, la preferencia
# guardada quedaba sin aplicarse en una instalación nueva porque la pista
# preferida todavía no estaba en la lista al único intento). Reintentar
# es inofensivo: repoblar los combos con la misma lista no cambia nada.
TRACKS_REFRESH_RETRY_DELAYS_MS = (300, 1000, 2500)
# Tope para llegar a "Playing" tras play() (mismo criterio que
# track_probe.PROBE_TIMEOUT_S, algo más generoso porque acá se abre el
# episodio real, no solo se lee un header). Necesario porque un open SMB
# que falla (ver CONTEXT.md: "Invalid argument" de libsmbclient contra
# ciertos servidores/redes) no siempre dispara MediaPlayerEncounteredError
# — el demux puede quedar reintentando decodificar para siempre sin pasar
# nunca a vlc.State.Error, así que no alcanza con escuchar ese evento solo.
PLAYBACK_OPEN_TIMEOUT_S = 20
# Medido con un archivo real (ver CONTEXT.md): al terminar de verdad,
# get_time()/get_length() da ~99.6% — 0.9 deja margen amplio de sobra. Un
# tiempo de reloj fijo no distingue "se vio completo" de "el usuario saltó
# con el scrubber cerca del final" — la posición real contra la duración
# real es la señal correcta, independiente de cuánto tiempo real pasó.
MIN_POSITION_FRACTION_FOR_AUTO_ADVANCE = 0.9
# Fallback solo para cuando get_length() no está disponible (<= 0).
MIN_PLAYBACK_SECONDS_FALLBACK = 60


def format_time(ms):
    if not ms or ms < 0:
        ms = 0
    total_seconds = ms // 1000
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


class _ScreenSaverInhibitor:
    """Inhibe el protector de pantalla/suspensión mientras el video está
    efectivamente reproduciéndose — reportado en vivo en modo TV (la
    pantalla se apagaba sola a mitad de un episodio), pero el problema es
    el mismo en escritorio (nada lo inhibía tampoco ahí), por eso vive acá
    y no en tv_player.py.

    org.freedesktop.ScreenSaver por DBus (implementado por kscreenlocker
    en KDE Plasma; también lo soportan GNOME/otros entornos) — NO es un
    portal de Flatpak (esos no necesitan permiso explícito), así que hace
    falta `--talk-name=org.freedesktop.ScreenSaver` en el manifest
    (flatpak/io.github.antarest47.Lunyx.yml, finish-args). Probado
    también con el portal org.freedesktop.portal.Desktop/Inhibit (el
    camino "más correcto" para una app sandboxeada), pero PySide6 no
    expone una forma directa de mandar el argumento `flags` como uint32
    (D-Bus rechaza el mensaje por mismatch de tipo, "expected «(sua{sv})»"
    — QtDBus en PySide6 no tiene un QVariant tipado invocable desde
    Python) — org.freedesktop.ScreenSaver.Inhibit(string, string) evita
    el problema por completo, los dos argumentos ya son strings.

    Todo falla en silencio (isValid() en False, o cualquier excepción) —
    nunca debe romper la reproducción si el bus de sesión no está
    disponible o el permiso no está otorgado (ej. corriendo fuera de
    Flatpak, en un venv de desarrollo sin sesión DBus real)."""

    SERVICE = "org.freedesktop.ScreenSaver"
    PATH = "/org/freedesktop/ScreenSaver"

    def __init__(self):
        self._cookie = None

    def _interface(self):
        try:
            iface = QDBusInterface(self.SERVICE, self.PATH, self.SERVICE, QDBusConnection.sessionBus())
        except Exception:
            return None
        return iface if iface.isValid() else None

    def inhibit(self):
        if self._cookie is not None:
            return
        iface = self._interface()
        if iface is None:
            return
        try:
            reply = iface.call("Inhibit", "Lunyx", "Reproduciendo video")
            args = reply.arguments()
        except Exception:
            return
        if args:
            self._cookie = args[0]

    def uninhibit(self):
        if self._cookie is None:
            return
        cookie, self._cookie = self._cookie, None
        iface = self._interface()
        if iface is None:
            return
        try:
            iface.call("UnInhibit", cookie)
        except Exception:
            pass


class PlayerEngine(QObject):
    """media_playing/media_end_reached: puente de threading — el callback
    de libVLC corre en un thread interno, nunca en el de Qt; emitir una
    Signal acá es lo que hace segura la entrega al thread principal
    (conexión "queued" implícita cuando emisor y receptor están en
    threads distintos). _media_paused/_media_stopped son el mismo puente,
    solo para la inhibición de protector de pantalla (ver
    _ScreenSaverInhibitor) — el DBus call de inhibit()/uninhibit() NO
    puede correr en el thread de libVLC, así que no alcanza con llamarlo
    directo desde _on_vlc_*_event como con el resto de esta clase: hace
    falta pasar SIEMPRE por una signal conectada a un método propio de
    PlayerEngine (QObject con thread affinity = el thread de Qt), nunca
    directo a un método de _screensaver (objeto Python plano, sin thread
    affinity que Qt pueda usar para decidir queued vs. directa)."""

    media_playing = Signal()
    media_end_reached = Signal()
    media_failed = Signal(str)  # ver PLAYBACK_OPEN_TIMEOUT_S — timeout de apertura o error real de libVLC
    _media_paused = Signal()
    _media_stopped = Signal()
    _media_error_event = Signal()  # puente de threading para MediaPlayerEncounteredError, mismo criterio que el resto de esta clase

    def __init__(self):
        super().__init__()
        self.vlc_instance = vlc.Instance()
        self.media_player = self.vlc_instance.media_player_new()
        self.media_player.audio_set_volume(DEFAULT_VOLUME)
        self._screensaver = _ScreenSaverInhibitor()
        self.media_playing.connect(self._on_playing_inhibit_screensaver)
        self.media_end_reached.connect(self._on_stopped_uninhibit_screensaver)
        self._media_paused.connect(self._on_stopped_uninhibit_screensaver)
        self._media_stopped.connect(self._on_stopped_uninhibit_screensaver)
        self._open_watchdog = QTimer(self)
        self._open_watchdog.setSingleShot(True)
        self._open_watchdog.timeout.connect(self._on_open_timeout)
        self.media_playing.connect(self._open_watchdog.stop)
        self._media_error_event.connect(self._on_open_timeout)
        events = self.media_player.event_manager()
        events.event_attach(vlc.EventType.MediaPlayerPlaying, self._on_vlc_playing_event)
        events.event_attach(vlc.EventType.MediaPlayerEndReached, self._on_vlc_end_event)
        events.event_attach(vlc.EventType.MediaPlayerPaused, self._on_vlc_paused_event)
        events.event_attach(vlc.EventType.MediaPlayerStopped, self._on_vlc_stopped_event)
        events.event_attach(vlc.EventType.MediaPlayerEncounteredError, self._on_vlc_error_event)

    def _on_vlc_playing_event(self, event):
        self.media_playing.emit()

    def _on_vlc_end_event(self, event):
        self.media_end_reached.emit()

    def _on_vlc_paused_event(self, event):
        self._media_paused.emit()

    def _on_vlc_stopped_event(self, event):
        self._media_stopped.emit()

    def _on_vlc_error_event(self, event):
        self._media_error_event.emit()

    def _on_open_timeout(self):
        # Guard: media_playing.connect(self._open_watchdog.stop) ya cubre
        # la carrera normal, pero MediaPlayerEncounteredError puede llegar
        # por la cola justo después de un Playing legítimo (ej. error de
        # una pista secundaria) — no tratar eso como apertura fallida.
        if self.media_player.get_state() == vlc.State.Playing:
            return
        self._open_watchdog.stop()
        self.media_player.stop()
        self.media_failed.emit(
            "No se pudo abrir el video — puede ser un problema de conexión con el servidor."
        )

    def _on_playing_inhibit_screensaver(self):
        self._screensaver.inhibit()

    def _on_stopped_uninhibit_screensaver(self):
        self._screensaver.uninhibit()

    def play_url(self, url, extra_options, xwindow_id):
        media = self.vlc_instance.media_new(url)
        for option in extra_options:
            media.add_option(option)
        self.media_player.set_media(media)
        # set_xwindow depende de X11/XWayland (ver CONTEXT.md). En sesión
        # Wayland pura hay que forzar QT_QPA_PLATFORM=xcb.
        self.media_player.set_xwindow(xwindow_id)
        self.media_player.play()
        self._open_watchdog.start(PLAYBACK_OPEN_TIMEOUT_S * 1000)


def resolve_previous_action(media_player, playlist, playlist_index):
    """Como en YouTube/Spotify: dentro de los primeros RESTART_THRESHOLD_MS
    salta al episodio anterior; pasado ese punto (o si no hay anterior, ej.
    primer episodio de la serie) reinicia el episodio actual. Devuelve
    ("change", subpath, title) o ("restart", None, None) — el caller
    decide qué hacer con cada caso (desktop emite una signal, TV llama a
    play directo)."""
    position = media_player.get_time() or 0
    if position <= RESTART_THRESHOLD_MS and playlist_index > 0:
        subpath, title = playlist[playlist_index - 1][:2]
        return ("change", subpath, title)
    return ("restart", None, None)


def resolve_auto_advance(media_player, playlist, playlist_index, play_started_at):
    """Devuelve ("advance", subpath, title) si corresponde auto-avanzar al
    siguiente episodio, ("back", None, None) si hay que volver (fin de
    temporada/serie, o el final no parece genuino), o None si no hay
    playlist (película u otro caso sin auto-avance) — el caller no debe
    hacer nada especial en ese caso.

    Salvaguarda contra un loop de auto-avance descontrolado: si libVLC
    reporta "fin" con la posición real todavía lejos de la duración real
    (fallas de decodificación en transiciones sucesivas de episodio
    pueden disparar MediaPlayerEndReached mucho antes del final real), no
    es confiable asumir que el episodio se vio completo. Se compara
    posición/duración REAL, no tiempo de reloj desde que arrancó — así no
    importa si el episodio se vio completo o si se saltó rápido con el
    scrubber hasta cerca del final, ambos casos son "fin genuino" igual."""
    if not playlist or not (0 <= playlist_index < len(playlist)):
        return None

    length = media_player.get_length() or 0
    position = media_player.get_time() or 0
    if length > 0:
        looks_genuine = position >= length * MIN_POSITION_FRACTION_FOR_AUTO_ADVANCE
    else:
        elapsed = time.monotonic() - play_started_at
        looks_genuine = elapsed >= MIN_PLAYBACK_SECONDS_FALLBACK
    if not looks_genuine:
        return ("back", None, None)

    current_group_key = playlist[playlist_index][2]
    next_index = playlist_index + 1
    if next_index < len(playlist):
        next_subpath, next_title, next_group_key, next_kind = playlist[next_index]
        # Solo avanza solo a otro EPISODIO REGULAR de la MISMA temporada —
        # nunca salta automático a un opening/ending/extra ni cruza a la
        # temporada siguiente (en esos casos hay que volver a la lista).
        if next_kind == "episode" and next_group_key == current_group_key:
            return ("advance", next_subpath, next_title)
    return ("back", None, None)


def build_track_picker_options(descriptions, current_track_id):
    """[(label, track_id), ...], selected_index — descriptions: lista de
    (track_id, description) tal cual las devuelve libVLC
    (audio_get_track_description()/video_get_spu_description()). Pura
    (no toca ningún widget): el caller decide cómo mostrarla (un
    QComboBox en desktop, un TVValuePicker en TV)."""
    options = []
    current_index = 0
    for i, (track_id, description) in enumerate(descriptions or []):
        label = description.decode("utf-8", errors="replace") if isinstance(description, bytes) else str(description)
        options.append((label, track_id))
        if track_id == current_track_id:
            current_index = i
    return options, current_index
