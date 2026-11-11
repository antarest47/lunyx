"""
Ventana raíz del modo TV (ver CONTEXT.md, "Modo TV"). Mismo patrón que
main.MainWindow: un QStackedWidget con galería, lista de episodios y
reproductor. Nunca reusa GalleryView/EpisodeListView/PlayerView de
desktop — vistas completamente separadas, ver razonamiento en CONTEXT.md.
"""

from PySide6.QtWidgets import QVBoxLayout, QWidget, QStackedWidget

import credentials
import smb_scanner
from tv_episodes import TVEpisodeListView
from tv_gallery import TVGalleryView
from tv_player import TVPlayerView


class TVMainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Lunyx (TV)")

        self.stack = QStackedWidget()
        self.gallery_view = TVGalleryView()
        self.episode_view = TVEpisodeListView(on_back=self._show_gallery)
        self.player_view = TVPlayerView()
        self.stack.addWidget(self.gallery_view)
        self.stack.addWidget(self.episode_view)
        self.stack.addWidget(self.player_view)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.stack)

        # biblioteca de la serie actualmente abierta en episode_view —
        # mismo rol que MainWindow._series_library en main.py.
        self._series_library = None
        self._player_return_view = self.gallery_view
        # subpath del episodio a saltar apenas termine el scan en curso —
        # mismo mecanismo que MainWindow._pending_resume_subpath.
        self._pending_resume_subpath = None

        self.gallery_view.play_requested.connect(self._play_movie)
        self.gallery_view.series_requested.connect(self._open_series)
        self.gallery_view.resume_episode_requested.connect(self._resume_series_episode)
        self.episode_view.episode_selected.connect(self._play_episode)
        self.episode_view.series_renamed.connect(self.gallery_view.update_series_name)
        self.episode_view.episodes_ready.connect(self._on_episode_list_ready)
        self.player_view.back_requested.connect(self._player_back)
        self.player_view.episode_change_requested.connect(self._play_episode_from_player)

    def _show_gallery(self):
        self.stack.setCurrentWidget(self.gallery_view)

    def _open_series(self, library, subpath, name):
        self._series_library = library
        self._pending_resume_subpath = None  # navegación normal — cancela cualquier salto pendiente de antes
        self.stack.setCurrentWidget(self.episode_view)
        self.episode_view.load(library, subpath, name)

    def _resume_series_episode(self, library, series_subpath, episode_subpath, fallback_title):
        # "Continuar viendo" pidió un salto DIRECTO al episodio, no a la
        # lista — mismo criterio que MainWindow._resume_series_episode.
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
        playlist = self.player_view.playlist
        index = next((i for i, item in enumerate(playlist) if item[0] == subpath), -1)
        self._play(
            self._series_library, subpath, title=title,
            playlist=playlist, playlist_index=index, series_key=self.player_view.series_key,
        )

    def _play(self, library, subpath, title, playlist=None, playlist_index=-1, series_key=None):
        password = credentials.get_password(library.id, library.username) or ""
        url, options = smb_scanner.playback_target(library, password, *subpath)
        # prepare_for_open() ANTES del setCurrentWidget — ver esa función
        # en tv_player.py: el propio cambio de página del stack ya
        # dispara un cambio de foco real que reabre el overlay completo
        # si no se suprime antes de que pase.
        self.player_view.prepare_for_open()
        self.stack.setCurrentWidget(self.player_view)
        self.player_view.play_url(
            url, title=title, library_id=library.id, subpath=subpath,
            extra_options=options, playlist=playlist, playlist_index=playlist_index,
            series_key=series_key, library=library,
        )

    def _player_back(self):
        # Capturado ANTES de stop_playback() (que lo borra) — pedido
        # explícito del usuario: al volver a la lista de episodios, tiene
        # que quedar resaltado el episodio que se estaba viendo (no el
        # que estaba seleccionado antes de entrar al reproductor, que
        # puede ser otro si hubo anterior/siguiente/auto-avance).
        last_subpath = self.player_view._current_subpath
        self.player_view.stop_playback()
        self.stack.setCurrentWidget(self._player_return_view)
        if self._player_return_view is self.episode_view:
            self.episode_view.reload(focus_subpath=last_subpath)
        elif self._player_return_view is self.gallery_view:
            self.gallery_view.refresh_continue_watching()
