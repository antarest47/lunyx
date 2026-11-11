"""
Sondeo de las pistas de audio/subtítulo REALES de un episodio.

Motivación: el combo "Audio/Subtítulos por defecto" del header de
episodes.py mostraba siempre los mismos presets genéricos
(Japonés/Inglés) sin importar si la serie realmente tenía esas pistas —
en el reproductor (main.py) sí se ven las pistas reales porque ahí se lee
un archivo ya abierto (ver _refresh_tracks). Este módulo hace lo mismo
pero de forma standalone, con una instancia de libVLC descartable
(--vout=dummy/--aout=dummy, sin ventana), solo para leer las
descripciones de pista de UN episodio representativo — las mismas
descripciones, en el mismo orden (incluyendo "Deshabilitar"), que vería
el usuario abriendo ESE episodio en el reproductor real. El resultado se
cachea server-side (library_metadata.set_track_probe), así que esto solo
corre una vez por serie, no en cada apertura.
"""

import threading
import time

import vlc

import smb_scanner

PROBE_TIMEOUT_S = 12
TRACKS_SETTLE_DELAY_S = 0.3  # mismo margen que main.py._on_media_playing_qt, por las dudas de que el demux no haya terminado de poblar las descripciones apenas entra en Playing


class ProbeError(Exception):
    pass


def pick_probe_subpath(groups):
    """Elige un episodio representativo de la serie para sondear pistas:
    preferí un episodio numerado normal (más probable que tenga todas las
    pistas de audio/subs reales de la serie); si no hay ninguno, cualquier
    extra/opening/ending sirve. None si la serie no tiene ningún video."""
    for bucket in ("episodes", "extras", "openings", "endings"):
        for group in groups:
            entries = getattr(group, bucket)
            if entries:
                return entries[0].subpath
    return None


def probe_tracks(library, password, episode_subpath):
    """Devuelve (audio_descriptions, subtitle_descriptions): listas de
    strings ya decodificados con las descripciones de pista reales, en el
    mismo orden que libVLC las expone (incluye "Deshabilitar")."""
    url, options = smb_scanner.playback_target(library, password, *episode_subpath)
    instance = vlc.Instance(["--vout=dummy", "--aout=dummy", "--quiet"])
    player = instance.media_player_new()
    media = instance.media_new(url)
    for option in options:
        media.add_option(option)
    player.set_media(media)

    reached = threading.Event()
    events = player.event_manager()
    events.event_attach(vlc.EventType.MediaPlayerPlaying, lambda e: reached.set())
    events.event_attach(vlc.EventType.MediaPlayerEncounteredError, lambda e: reached.set())

    try:
        player.play()
        if not reached.wait(PROBE_TIMEOUT_S):
            raise ProbeError(f"Timeout sondeando pistas de {episode_subpath}")
        if player.get_state() == vlc.State.Error:
            raise ProbeError(f"libVLC no pudo abrir {episode_subpath} para sondear pistas")
        time.sleep(TRACKS_SETTLE_DELAY_S)
        audio_descriptions = _decode_descriptions(player.audio_get_track_description())
        subtitle_descriptions = _decode_descriptions(player.video_get_spu_description())
    finally:
        player.stop()
        player.release()
        media.release()
        instance.release()

    return audio_descriptions, subtitle_descriptions


def _decode_descriptions(descriptions):
    texts = []
    for _track_id, description in descriptions or []:
        text = description.decode("utf-8", errors="replace") if isinstance(description, bytes) else str(description)
        texts.append(text)
    return texts
