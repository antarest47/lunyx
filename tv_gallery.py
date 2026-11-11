"""
Galería del modo TV — Paso 2 de la iniciativa "modo TV" (ver CONTEXT.md).
Clase completamente separada de gallery.GalleryView, a propósito: nunca
comparten widgets con un flag de modo (ver razonamiento en CONTEXT.md,
sección "Modo TV"). Lo que SÍ se reusa es todo lo que es agnóstico de
vista: los QThread workers de gallery.py (ScanWorker, ProgressRefreshWorker),
las funciones puras de módulo (has_resolvable_progress,
resolve_continue_watching_entries, _rounded_pixmap, _zoomed_pixmap,
_elide_two_lines), y ManageLibrariesDialog completo.

Navegable solo con teclado (flechas + Enter) — ver tv_input.py. A
diferencia de gallery.PosterTile (pensado para mouse, sin foco de
teclado), TVPosterTile es focusable y responde a Enter.
"""

from pathlib import Path

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QFont, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QGraphicsDropShadowEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

import app_settings
import gallery
import library_store
import theme
import tv_input

TV_POSTER_W, TV_POSTER_H = 220, 320
TV_POSTER_RADIUS = 10
TV_GRID_COLUMNS = 4
TV_FOCUS_BORDER = 4  # más grueso que el hover de escritorio (2px) — a distancia de sillón tiene que notarse
TV_LOGO_SIZE = 48  # escalado del LOGO_SIZE=36 de escritorio, misma proporción que TV_POSTER_W/POSTER_W


class TVPosterTile(QWidget):
    def __init__(self, name, pixmap, on_click, progress_fraction=None, badge_text=None):
        super().__init__()
        self._on_click = on_click
        self._source_pixmap = pixmap if (pixmap and not pixmap.isNull()) else None
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(8, 8, 8, 8)
        outer_layout.setSpacing(0)

        # Mismo truco de dos piezas que gallery.PosterTile: carátula con
        # esquinas de ARRIBA redondeadas (pixmap recortado) + franja de
        # título con las de ABAJO redondeadas por QSS — el contorno
        # combinado da un rectángulo parejo para el borde de foco.
        self.card = QWidget()
        self.card.setObjectName("tvPosterCard")
        self.card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._card_style_base = (
            f"QWidget#tvPosterCard {{ border: {TV_FOCUS_BORDER}px solid transparent; "
            f"border-radius: {TV_POSTER_RADIUS}px; background: transparent; }}"
        )
        self._card_style_focused = (
            f"QWidget#tvPosterCard {{ border: {TV_FOCUS_BORDER}px solid {theme.ACCENT}; "
            f"border-radius: {TV_POSTER_RADIUS}px; background: transparent; }}"
        )
        self.card.setStyleSheet(self._card_style_base)

        card_layout = QVBoxLayout(self.card)
        card_layout.setContentsMargins(TV_FOCUS_BORDER, TV_FOCUS_BORDER, TV_FOCUS_BORDER, TV_FOCUS_BORDER)
        card_layout.setSpacing(0)

        self.poster_label = QLabel()
        self.poster_label.setFixedSize(TV_POSTER_W, TV_POSTER_H)
        self.poster_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        if self._source_pixmap:
            self.poster_label.setPixmap(
                gallery._zoomed_pixmap(
                    self._source_pixmap, TV_POSTER_W, TV_POSTER_H, 1.0, TV_POSTER_RADIUS, corners="top"
                )
            )
            self.poster_label.setStyleSheet("background: transparent; border: none;")
        else:
            self.poster_label.setText("Sin\nposter")
            self.poster_label.setStyleSheet(
                f"background-color: #23262e; color: #6b7078; font-size: 16px; "
                f"border-top-left-radius: {TV_POSTER_RADIUS}px; border-top-right-radius: {TV_POSTER_RADIUS}px;"
            )

        if progress_fraction and self._source_pixmap:
            bar_width = round(TV_POSTER_W * max(0.0, min(1.0, progress_fraction)))
            progress_bar = QWidget(self.poster_label)
            progress_bar.setStyleSheet(f"background-color: {theme.ACCENT}; border: none;")
            progress_bar.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            progress_bar.setGeometry(0, TV_POSTER_H - 4, bar_width, 4)
            progress_bar.raise_()

        if badge_text:
            # Solo aparece en resultados de búsqueda cross-library, mismo
            # criterio que gallery.PosterTile — a escala TV (fuente más
            # grande, se ve a distancia de sillón).
            badge = QLabel(badge_text, self.poster_label)
            badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            badge.setStyleSheet(
                f"background-color: rgba(10, 11, 13, 0.8); color: {theme.ACCENT}; "
                "font-size: 13px; font-weight: 600; border-radius: 5px; padding: 3px 8px;"
            )
            badge.adjustSize()
            badge.move(8, 8)
            badge.raise_()

        card_layout.addWidget(self.poster_label)

        name_label = QLabel()
        name_label.setObjectName("tvPosterTitle")
        name_label.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        name_label.setStyleSheet(
            f"background-color: {theme.SURFACE}; color: #c6c9d1; font-size: 15px; "
            f"border-bottom-left-radius: {TV_POSTER_RADIUS}px; border-bottom-right-radius: {TV_POSTER_RADIUS}px; "
            "padding: 8px 6px;"
        )
        measure_font = QFont(name_label.font())
        measure_font.setPixelSize(15)
        name_label.setText(gallery._elide_two_lines(name, measure_font, TV_POSTER_W - 8))
        name_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        name_label.setFixedWidth(TV_POSTER_W)
        name_label.setFixedHeight(42 + 16)
        card_layout.addWidget(name_label)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(28)
        shadow.setOffset(0, 4)
        shadow.setColor(Qt.GlobalColor.black)
        self.card.setGraphicsEffect(shadow)

        outer_layout.addWidget(self.card)

    def focusInEvent(self, event):
        self.card.setStyleSheet(self._card_style_focused)
        super().focusInEvent(event)

    def focusOutEvent(self, event):
        self.card.setStyleSheet(self._card_style_base)
        super().focusOutEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Select):
            self._on_click()
            return
        super().keyPressEvent(event)


class TVGalleryView(QWidget):
    """Equivalente de gallery.GalleryView para modo TV — bibliotecas +
    "Continuar viendo", sin buscador ni orden (no tienen sentido
    navegando solo con flechas/D-pad, sin teclado en pantalla). Mismo
    contrato de señales que GalleryView, para que TVMainWindow (y los
    Pasos 3/4 más adelante) las conecte con la misma forma de datos que
    ya usa el resto de la app."""

    play_requested = Signal(object, tuple, str)
    series_requested = Signal(object, tuple, str)
    resume_episode_requested = Signal(object, tuple, tuple, str)

    def __init__(self):
        super().__init__()
        self._scan_worker = None
        self._progress_refresh_worker = None
        self._pending_default_continue_tab = False
        self._current_library = None
        self._current_items = []
        self._viewing_continue_watching = False
        # Estado propio, NO compartido con GalleryView — modo TV y modo
        # desktop son procesos distintos (ver app_restart.py), nunca
        # coexisten, así que no hay instancia de GalleryView de la que
        # "heredar" nada acá.
        self._items_cache = {}
        self._season_search_cache = {}  # library.id -> {subpath: [nombre_temporada, ...]}
        self._library_by_id = {}
        self._tab_buttons = {}
        self._grid_tiles = {}  # (row, col) -> TVPosterTile, ver _poster_focus_override
        self._prescan_queue = []
        self._prescan_running = False
        self._background_workers = []
        self._did_initial_focus = False
        self._exit_confirm_pending = False

        self.logo_label = QLabel()
        self.logo_label.setFixedSize(TV_LOGO_SIZE, TV_LOGO_SIZE)
        logo_path = Path(__file__).parent / "logo.png"
        logo_pixmap = QPixmap(str(logo_path))
        if not logo_pixmap.isNull():
            scaled_logo = logo_pixmap.scaled(
                TV_LOGO_SIZE, TV_LOGO_SIZE,
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            self.logo_label.setPixmap(gallery._rounded_pixmap(scaled_logo, TV_LOGO_SIZE // 2))

        self.title_label = QLabel(app_settings.get_collection_title())
        self.title_label.setStyleSheet("font-size: 26px; font-weight: 700; color: #f2f3f5;")

        # Reusa gallery.SearchLineEdit tal cual (no es un widget de VISTA,
        # es una subclase chica de QLineEdit cuyo único comportamiento
        # extra es limpiarse con Escape) — filtra client-side sobre lo ya
        # escaneado en _items_cache, igual que en desktop (ver
        # _render_grid), sin volver a pegarle a SMB.
        self.search_input = gallery.SearchLineEdit()
        self.search_input.setObjectName("tvSearchInput")
        self.search_input.setPlaceholderText("Buscar series o episodios...")
        self.search_input.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.search_input.textChanged.connect(self._on_search_changed)
        # Inverso del override "up" de los posters (ver
        # _poster_focus_override): Abajo desde el buscador va a la
        # pestaña activa, no al primer poster por cercanía geométrica.
        self.search_input._tv_focus_override = {"down": self._active_tab_button}

        # "Actualizar biblioteca" — existía en desktop (gallery.GalleryView)
        # pero faltaba en TV; ver CONTEXT.md Paso 33. Mismo handler
        # (_refresh_current_library) que desktop reusa tal cual.
        self.refresh_button = QPushButton("Actualizar biblioteca")
        self.refresh_button.setObjectName("tvManageButton")
        self.refresh_button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.refresh_button.setAutoDefault(True)  # ver nota en manage_button más abajo
        self.refresh_button.clicked.connect(self._refresh_current_library)

        self.manage_button = QPushButton("Gestionar bibliotecas")
        self.manage_button.setObjectName("tvManageButton")
        self.manage_button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        # QPushButton solo reacciona a Enter/Return vía su propio
        # keyPressEvent si autoDefault (o default) está activado — por
        # default eso es True adentro de un QDialog, pero False en un
        # QWidget suelto como esta vista. Sin esto, Enter no hacía nada
        # en ningún botón de la galería de TV (probado, no es teórico).
        self.manage_button.setAutoDefault(True)
        self.manage_button.clicked.connect(self._open_manage_dialog)

        top_row = QHBoxLayout()
        top_row.setSpacing(16)
        top_row.addWidget(self.logo_label)
        top_row.addWidget(self.title_label)
        top_row.addWidget(self.search_input, stretch=1)
        top_row.addWidget(self.refresh_button)
        top_row.addWidget(self.manage_button)

        self.tabs_layout = QHBoxLayout()
        self.tabs_layout.setContentsMargins(10, 10, 10, 10)
        self.tabs_layout.setSpacing(10)
        self.tabs_layout.addStretch()
        # Mismo patrón que GalleryView.library_button_group (desktop) —
        # sin esto, tildar un tab no destilda al anterior, y quedaban
        # varios/ningún tab con el estilo ":checked" a la vez (bug real
        # reportado en vivo tras el Paso 28).
        self.tab_button_group = QButtonGroup(self)
        self.tab_button_group.setExclusive(True)

        tabs_bar = QWidget()
        tabs_bar.setObjectName("tabsBar")
        tabs_bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        tabs_bar.setLayout(self.tabs_layout)

        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: #8a8f99; font-size: 14px;")

        self.grid_container = QWidget()
        self.grid_layout = QGridLayout(self.grid_container)
        self.grid_layout.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
        self.grid_layout.setSpacing(6)

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setWidget(self.grid_container)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 24, 32, 24)
        layout.setSpacing(14)
        layout.addLayout(top_row)
        layout.addWidget(tabs_bar)
        layout.addWidget(self.status_label)
        layout.addWidget(self.scroll_area)

        # Aviso flotante de "doble Esc para salir" (ver _handle_exit_escape
        # más abajo) — hijo directo de `self`, FUERA de cualquier layout
        # (para no correr el resto de la vista al aparecer/desaparecer),
        # posicionado a mano en _show_exit_hint().
        self.exit_hint_label = QLabel("Presioná Esc otra vez para salir", self)
        self.exit_hint_label.setObjectName("tvExitHint")
        self.exit_hint_label.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.exit_hint_label.setStyleSheet(f"""
            QLabel#tvExitHint {{
                background-color: rgba(10, 11, 13, 0.92);
                color: {theme.ACCENT};
                font-size: 20px;
                font-weight: 600;
                border-radius: 10px;
                border: 2px solid {theme.ACCENT};
                padding: 16px 32px;
            }}
        """)
        self.exit_hint_label.hide()

        self._exit_confirm_timer = QTimer(self)
        self._exit_confirm_timer.setSingleShot(True)
        self._exit_confirm_timer.setInterval(3000)
        self._exit_confirm_timer.timeout.connect(self._cancel_exit_confirm)

        # Estilo propio (objectName distinto de "libraryTab"/desktop a
        # propósito — nunca tocar theme.py para esto, ver principio de
        # "TV no comparte widgets ni estilos con desktop"). El agregado
        # real acá es el ":focus" — sin un borde propio, el QSS con
        # background-color/border custom de estos botones apaga el
        # rectángulo de foco nativo de Qt, así que sin esto el foco de
        # teclado era invisible aunque funcionara (bug real encontrado en
        # vivo: se podía "estar" en el botón de Gestionar bibliotecas sin
        # ninguna señal visual, solo se notaba al apretar Enter).
        #
        # El ANCHO del borde es 3px en TODOS los estados (base/:checked/
        # :focus) a propósito, ni un pixel distinto entre ellos — solo el
        # COLOR cambia. Antes :focus usaba 3px pero base/:checked usaban
        # 2px, así que cada vez que el foco entraba o salía de una pestaña
        # (constante en TV, se navega solo con flechas) el borde
        # engordaba/adelgazaba y el cuadro de contenido (padding + borde)
        # se corría un pixel — eso reportó el usuario probando en vivo como
        # texto "roto"/fantasma en las pestañas (glitch de repintado real,
        # no algo cosmético menor: confirmado ausente en desktop, cuyo
        # #libraryTab equivalente en theme.py no tiene ningún ":focus" que
        # cambie el borde, porque ahí se navega con mouse). Ancho fijo =
        # geometría del botón nunca cambia entre estados = nada que
        # repintar mal.
        self.setStyleSheet(f"""
            QPushButton#tvLibraryTab {{
                background-color: {theme.BG};
                border: 3px solid transparent;
                border-radius: 8px;
                padding: 10px 20px;
                color: {theme.TEXT_DIM};
                font-weight: 600;
                font-size: 16px;
            }}
            QPushButton#tvLibraryTab:checked {{
                background-color: rgba({theme.ACCENT_RGB}, 0.15);
                color: {theme.ACCENT};
                border: 3px solid {theme.ACCENT};
            }}
            QPushButton#tvLibraryTab:focus {{
                border: 3px solid {theme.ACCENT};
            }}
            QPushButton#tvManageButton {{
                background-color: {theme.SURFACE};
                border: 3px solid transparent;
                border-radius: 8px;
                padding: 8px 18px;
                color: {theme.TEXT_DIM};
                font-size: 14px;
            }}
            QPushButton#tvManageButton:focus {{
                border: 3px solid {theme.ACCENT};
                color: {theme.ACCENT};
            }}
            QLineEdit#tvSearchInput {{
                font-size: 16px;
                padding: 9px 14px;
            }}
        """)

        # Navegación espacial sobre TODA la vista — tabs, botón de
        # gestión y grilla comparten el mismo dominio de foco, sin
        # distinguir "zonas" a mano (ver tv_input.install_spatial_navigation).
        tv_input.install_spatial_navigation(self)
        # Escape tiene dos comportamientos en esta vista, según dónde esté
        # el foco (install_escape_back dispara con el foco en cualquier
        # lado de la vista, así que el callback mismo distingue los casos
        # en vez de instalar un filtro nuevo por caso):
        # - En un poster: mismo destino que Arriba, a la pestaña activa
        #   (ver _poster_focus_override) — pedido explícito.
        # - En una pestaña (o "Gestionar bibliotecas"): esta vista es la
        #   raíz, no hay "volver" más atrás — dispara la confirmación de
        #   salida (ver _handle_exit_escape), pedido explícito para poder
        #   cerrar la app sin teclado/mouse (Steam Big Picture en Bazzite).
        tv_input.install_escape_back(self, self._on_escape)

        # Bug real reportado en vivo: navegar con flechas movía el foco
        # correctamente, pero la grilla no scrolleaba para seguirlo — el
        # poster recién enfocado podía quedar fuera del viewport visible.
        # QApplication.focusChanged (no un eventFilter local) porque el
        # foco puede caer en cualquier hijo de grid_container (un
        # TVPosterTile) y este es el único signal que se entera de
        # cualquier cambio de foco en toda la app, sin acoplar
        # TVPosterTile a una referencia del scroll area.
        QApplication.instance().focusChanged.connect(self._on_focus_changed_scroll)

        self.reload_libraries()

    def _on_focus_changed_scroll(self, old, now):
        if now is not None and self.grid_container.isAncestorOf(now):
            self.scroll_area.ensureWidgetVisible(now, 20, 20)

    def _on_escape(self):
        focused = QApplication.focusWidget()
        if isinstance(focused, TVPosterTile):
            # Este Escape se usa para volver a la pestaña, no para salir —
            # si había un aviso de salida pendiente de un Escape anterior,
            # se cancela: este gesto ya se "gastó" en otra cosa.
            if self._exit_confirm_pending:
                self._exit_confirm_timer.stop()
                self._cancel_exit_confirm()
            self._active_tab_button().setFocus()
            return
        if focused in self._top_row_widgets():
            self._handle_exit_escape()
        # Cualquier otro caso (ej. buscador vacío): nada que hacer acá.

    def _top_row_widgets(self):
        widgets = [self._continue_watching_button, self.manage_button]
        widgets.extend(self._tab_buttons.values())
        return widgets

    def _handle_exit_escape(self):
        """Primer Escape sobre una pestaña: muestra el aviso y arranca el
        timer de 3s. Segundo Escape ANTES de que el timer expire: cierra
        la ventana de verdad — window.close() (no app.quit() directo) para
        que dispare el mismo camino de cierre que ya existe (aboutToQuit
        -> _save_progress_and_wait, ver main.py), sin duplicar esa
        lógica acá. Si el timer ya expiró, _cancel_exit_confirm() dejó
        _exit_confirm_pending en False, así que este Escape vuelve a
        contarse como el primero (reinicia el ciclo, pedido explícito)."""
        if self._exit_confirm_pending:
            self._exit_confirm_timer.stop()
            self._exit_confirm_pending = False
            self.exit_hint_label.hide()
            self.window().close()
            return
        self._exit_confirm_pending = True
        self._show_exit_hint()
        self._exit_confirm_timer.start()

    def _show_exit_hint(self):
        self.exit_hint_label.adjustSize()
        x = (self.width() - self.exit_hint_label.width()) // 2
        self.exit_hint_label.move(x, 40)
        self.exit_hint_label.show()
        self.exit_hint_label.raise_()

    def _cancel_exit_confirm(self):
        self._exit_confirm_pending = False
        self.exit_hint_label.hide()

    # ---- carga de bibliotecas (mismo criterio que GalleryView.reload_libraries) ----

    def reload_libraries(self):
        self._clear_tabs()
        libraries = library_store.list_libraries()
        self._library_by_id = {library.id: library for library in libraries}
        self._tab_buttons = {}

        self._continue_watching_button = QPushButton("Continuar viendo")
        self._continue_watching_button.setObjectName("tvLibraryTab")
        self._continue_watching_button.setCheckable(True)
        self._continue_watching_button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._continue_watching_button.setAutoDefault(True)  # ver nota en manage_button más arriba
        self._continue_watching_button.clicked.connect(self._on_continue_watching_tab_selected)
        self.tab_button_group.addButton(self._continue_watching_button)
        self.tabs_layout.insertWidget(self.tabs_layout.count() - 1, self._continue_watching_button)
        self._update_continue_watching_tab_visibility()

        for library in libraries:
            button = QPushButton(library.name)
            button.setObjectName("tvLibraryTab")
            button.setCheckable(True)
            button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            button.setAutoDefault(True)  # ver nota en manage_button más arriba
            button.clicked.connect(lambda checked, lib=library: self._on_library_tab_clicked(lib))
            self.tab_button_group.addButton(button)
            self.tabs_layout.insertWidget(self.tabs_layout.count() - 1, button)
            self._tab_buttons[library.id] = button

        if self._has_resolvable_progress() and libraries:
            self._continue_watching_button.setChecked(True)
            self._on_continue_watching_tab_selected()
            self._start_background_prescan(libraries)
        elif libraries:
            self._pending_default_continue_tab = any(
                getattr(lib, "progress_centralized", False) for lib in libraries
            )
            self._tab_buttons[libraries[0].id].setChecked(True)
            self._on_library_selected(libraries[0])
            self._start_background_prescan(libraries[1:])
        else:
            self._current_library = None
            self._current_items = []
            self._clear_grid()
            self.status_label.setText("No hay bibliotecas configuradas todavía.")

        # Si esta es la carga INICIAL (todavía no se mostró la ventana),
        # esto es un no-op inofensivo — showEvent() se encarga apenas se
        # muestre (setFocus() antes del primer show() no "pega"). Si
        # reload_libraries() se llama de nuevo más tarde (volver de
        # "Gestionar bibliotecas" habiendo agregado/editado/borrado algo,
        # ver _open_manage_dialog) hace falta el mismo singleShot(0,...)
        # que _open_manage_dialog usa para el caso "sin cambios": recién
        # después de dialog.exec() la desactivación de ventana del
        # diálogo todavía está en danza, y un setFocus() síncrono en ese
        # momento se pisa solo — sin esto, los botones de tab se
        # reconstruyen (los viejos ya no existen) y nada quedaba
        # enfocado, dejando la navegación de flechas muerta (bug real
        # encontrado en vivo).
        self._rewire_top_row_navigation()
        QTimer.singleShot(0, self._focus_default_tab)

    def _rewire_top_row_navigation(self):
        """Encadena "Continuar viendo" + tabs de biblioteca + "Actualizar
        biblioteca" + "Gestionar bibliotecas" como UNA sola fila para
        Izquierda/Derecha (ver tv_input.TV_FOCUS_OVERRIDE_ATTR), sin
        depender de la búsqueda geométrica — bug real reportado en vivo:
        Derecha en la última pestaña terminaba en el buscador (que vive en
        la fila de arriba, pero podía quedar geométricamente "más cerca"
        que el botón de gestión) en vez de en "Gestionar bibliotecas". Los
        extremos de la fila quedan con override explícito a None (bloquea
        el movimiento en esa dirección) en vez de sin override (que
        caería a la búsqueda geométrica) — la fila se comporta como un
        carril cerrado, previsible de punta a punta. Se re-arma cada vez
        que las pestañas cambian (reload_libraries) o que "Continuar
        viendo" aparece/desaparece
        (_update_continue_watching_tab_visibility).

        "Actualizar biblioteca" (agregado en el Paso 33) vive en el
        `top_row` de arriba, NO en esta fila de pestañas — pero tiene que
        quedar alcanzable desde acá igual: sin agregarlo al carril, Derecha
        en la última pestaña saltaba directo a "Gestionar bibliotecas"
        (bug real reportado en vivo), y desde "Gestionar" Izquierda volvía
        directo a la pestaña — "Actualizar" quedaba inalcanzable con
        flechas. Insertado ENTRE la última pestaña y "Gestionar", en el
        mismo orden visual que tienen en top_row."""
        row = []
        if self._continue_watching_button.isVisible():
            row.append(self._continue_watching_button)
        row.extend(self._tab_buttons.values())
        row.append(self.refresh_button)
        row.append(self.manage_button)
        for i, widget in enumerate(row):
            overrides = dict(getattr(widget, tv_input.TV_FOCUS_OVERRIDE_ATTR, None) or {})
            overrides["right"] = row[i + 1] if i + 1 < len(row) else None
            overrides["left"] = row[i - 1] if i > 0 else None
            widget._tv_focus_override = overrides

    def _start_background_prescan(self, libraries):
        self._prescan_queue.extend(lib for lib in libraries if lib.id not in self._items_cache)
        if not self._prescan_running:
            self._run_next_prescan()

    def _run_next_prescan(self):
        if not self._prescan_queue:
            self._prescan_running = False
            return
        self._prescan_running = True
        library = self._prescan_queue.pop(0)
        worker = gallery.ScanWorker(library)
        worker.finished_ok.connect(
            lambda items, season_search, lib=library: self._on_prescan_ok(lib, items, season_search)
        )
        worker.failed.connect(lambda _msg: self._run_next_prescan())
        self._background_workers.append(worker)
        worker.start()

    def _on_prescan_ok(self, library, items, season_search):
        self._items_cache[library.id] = items
        self._season_search_cache[library.id] = season_search
        if self._viewing_continue_watching:
            self._render_grid()
        self._reconcile_continue_watching_after_load()
        self._run_next_prescan()

    def _reconcile_continue_watching_after_load(self):
        has_progress = self._update_continue_watching_tab_visibility()
        if not self._pending_default_continue_tab:
            return
        if not has_progress:
            return
        self._pending_default_continue_tab = False
        self._continue_watching_button.setChecked(True)
        self._on_continue_watching_tab_selected()
        self._focus_default_tab()

    def showEvent(self, event):
        # setFocus() llamado ANTES de que la ventana se muestre por
        # primera vez no "pega" — Qt le reasigna el foco al widget que
        # corresponda según su propia lógica recién cuando la ventana se
        # activa, pisando cualquier setFocus() hecho durante __init__.
        # Por eso el foco inicial se fija acá, no en reload_libraries().
        super().showEvent(event)
        if not self._did_initial_focus:
            self._did_initial_focus = True
            self._focus_default_tab()

    def _focus_default_tab(self):
        self._reactivate_and_focus(self._active_tab_button())

    def _active_tab_button(self):
        """La pestaña que representa el estado actual de la vista —
        "Continuar viendo" si se está viendo esa grilla, la de la
        biblioteca actual si no, o el botón de gestión como último
        fallback (biblioteca recién creada sin tabs todavía). Usado tanto
        para el foco inicial (_focus_default_tab) como para el override
        "Arriba" de cada poster de la grilla (ver _render_grid) — así
        Arriba SIEMPRE lleva a la pestaña activa sin importar la columna,
        en vez de a lo que la búsqueda geométrica considere "más cerca"
        (que podía terminar siendo el buscador, bug real reportado en
        vivo)."""
        if self._viewing_continue_watching and self._continue_watching_button.isVisible():
            return self._continue_watching_button
        if self._current_library is not None:
            button = self._tab_buttons.get(self._current_library.id)
            if button:
                return button
        # Sin bibliotecas configuradas (o ningún caso de arriba aplicó):
        # el botón de gestión es lo único que queda para navegar — sin
        # este fallback, una TVGalleryView recién creada sin bibliotecas
        # dejaría el teclado completamente muerto, sin forma de llegar a
        # "Gestionar bibliotecas" para agregar la primera.
        return self.manage_button

    def _reactivate_and_focus(self, widget):
        """setFocus() a secas no alcanza en un momento específico: justo
        después de que un QDialog modal se cierra, la ventana contenedora
        puede quedar sin ser la "ventana activa" de Qt por un instante —
        mientras eso pase, setFocus() no tiene ningún efecto (confirmado
        con foco quedando en None incluso justo después de llamarlo). Se
        reactiva la ventana a mano en vez de confiar en que el gestor de
        ventanas lo haga solo — patrón estándar de Qt para este caso, y
        el fix real del bug de "las flechas quedan muertas después de
        cerrar Gestionar bibliotecas" encontrado en vivo."""
        window = self.window()
        window.raise_()
        window.activateWindow()
        widget.setFocus()

    def _clear_tabs(self):
        while self.tabs_layout.count() > 1:  # deja el addStretch() final
            item = self.tabs_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

    def _open_manage_dialog(self):
        dialog = gallery.ManageLibrariesDialog(self)
        tv_input.install_arrow_navigation(dialog)
        dialog.exec()
        self.title_label.setText(app_settings.get_collection_title())
        if dialog.changed:
            self.reload_libraries()  # ya deja el foco en un tab válido, ver el final de reload_libraries()
        else:
            # Nada cambió — bug real encontrado en vivo: al cerrar con
            # Escape sin cambiar nada, las flechas quedaban completamente
            # muertas (nada tenía foco real). setFocus() a secas acá no
            # alcanza (ver _reactivate_and_focus) — hace falta reactivar
            # la ventana a mano, Y encima diferirlo con singleShot(0, ...)
            # porque, justo cuando dialog.exec() recién retorna, Qt
            # todavía tiene pendiente procesar la desactivación de
            # ventana del diálogo que se está cerrando.
            QTimer.singleShot(0, lambda: self._reactivate_and_focus(self.manage_button))

    def _on_library_tab_clicked(self, library):
        self._pending_default_continue_tab = False
        self._on_library_selected(library)

    def _on_library_selected(self, library, force_rescan=False):
        self._current_library = library
        self._viewing_continue_watching = False

        cached = self._items_cache.get(library.id)
        if cached is not None and not force_rescan:
            self._current_items = cached
            self._render_grid()
            return

        self._current_items = []
        self._clear_grid()
        location = library.path if library.connection_type == "local" else f"{library.server}/{library.share}"
        self.status_label.setText(f"Escaneando {location}...")

        self._scan_worker = gallery.ScanWorker(library)
        self._scan_worker.finished_ok.connect(
            lambda items, season_search, lib=library: self._on_scan_ok(lib, items, season_search)
        )
        self._scan_worker.failed.connect(self._on_scan_failed)
        self._scan_worker.start()

    def _on_scan_ok(self, library, items, season_search):
        self._items_cache[library.id] = items
        self._season_search_cache[library.id] = season_search
        if self._current_library and self._current_library.id == library.id:
            self._current_items = items
            self._render_grid()
        elif self._viewing_continue_watching:
            self._render_grid()
        self._reconcile_continue_watching_after_load()

    def _on_scan_failed(self, message):
        self.status_label.setText(message)

    # ---- grilla ----

    def _render_grid(self):
        # Sin búsqueda: solo la biblioteca actual (datos ya en memoria).
        # Con búsqueda: cruza TODAS las bibliotecas ya precargadas en
        # _items_cache (ver _start_background_prescan), matcheando nombre
        # mostrado, nombre crudo, y nombres de temporada — mismo criterio
        # que gallery.GalleryView._render_grid (gallery.matches_search_query,
        # reusada tal cual).
        query = gallery._normalize_search(self.search_input.text().strip())
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
            season_names_by_lib = self._season_search_cache
            items = [
                entry for entry in pool
                if gallery.matches_search_query(
                    query, entry[0], entry[3],
                    season_names_by_lib.get(entry[5], {}).get(entry[1], ()),
                )
            ]
            # entry[4] (sort_key) — ver gallery.GalleryView._render_grid,
            # mismo motivo (respeta el orden custom de películas en vez
            # del alfabético por título).
            items = sorted(items, key=lambda entry: entry[4].lower())
            multi_library = len(self._items_cache) > 1
            scope = " en todas las bibliotecas" if multi_library else ""
            self.status_label.setText(f"{len(items)} de {len(pool)} ítems encontrados{scope}.")
        else:
            current_lib_id = self._current_library.id if self._current_library else None
            items = [
                (name, subpath, poster_bytes, raw_name, sort_key, current_lib_id)
                for (name, subpath, poster_bytes, raw_name, sort_key) in self._current_items
            ]
            # Antes esta rama no ordenaba nada (quedaba tal cual la
            # devolvía smb_scanner.scan_movies/scan_series, ya alfabético
            # por nombre de archivo/carpeta) — se agrega el mismo sort por
            # sort_key que la rama de búsqueda de arriba, sin cambiar el
            # resultado para nadie que no haya seteado un nombre de
            # ordenación custom (sort_key cae al nombre mostrado, que a su
            # vez cae al nombre crudo — mismo orden de siempre en ese
            # caso), pero ahora sí respeta el orden custom de películas.
            items = sorted(items, key=lambda entry: entry[4].lower())
            multi_library = False
            self.status_label.setText(f"{len(items)} ítems encontrados.")

        self._clear_grid()
        for i, (name, subpath, poster_bytes, raw_name, _sort_key, lib_id) in enumerate(items):
            library = self._library_by_id.get(lib_id)
            pixmap = QPixmap()
            if poster_bytes:
                pixmap.loadFromData(poster_bytes)
            badge = library.name if (cross_library and multi_library and library) else None
            tile = TVPosterTile(
                name, pixmap, badge_text=badge,
                on_click=lambda lib=library, sp=subpath, n=name: self._on_item_clicked(lib, sp, n),
            )
            row, col = divmod(i, TV_GRID_COLUMNS)
            is_last_in_row = col == TV_GRID_COLUMNS - 1 or i == len(items) - 1
            tile._tv_focus_override = self._poster_focus_override(row, col, is_last_in_row)
            self.grid_layout.addWidget(tile, row, col)
            self._grid_tiles[(row, col)] = tile

    def _poster_focus_override(self, row, col, is_last_in_row):
        """La grilla de posters es su propio "bloque" contenido:
        Izquierda/Derecha quedan atrapados en los bordes de cada fila
        (columna 0 / última columna real, que puede ser antes de
        TV_GRID_COLUMNS-1 si es la fila incompleta del final) — antes se
        "escapaban" hacia arriba (pestañas) o hacia el botón de gestión
        por pura cercanía geométrica, confuso según el usuario ("el
        movimiento debería quedar contenido en el bloque"). Arriba SOLO
        escapa a la pestaña activa desde la PRIMERA fila (row == 0).

        Filas != 0 apuntan con un override EXPLÍCITO al tile de
        `self._grid_tiles` en (row-1, col) — bug real reportado en vivo
        (con una biblioteca de 21 ítems, scrolleada varias filas hacia
        abajo): dejarlas caer al cálculo geométrico normal (como se hacía
        antes, ver comentario viejo más abajo) se rompe con el scroll.
        `_move_focus` (tv_input.py) busca candidatos sobre TODA la vista
        (pestañas, buscador, grilla completa) usando `mapTo(container,
        ...)`, que sí refleja la posición real ya scrolleada — así que
        cuanto más abajo está scrolleada la grilla, más chica es la
        distancia (en coordenadas de la vista) entre el poster enfocado y
        la barra de pestañas (que NO se mueve, vive fuera del
        QScrollArea), mientras que la distancia entre filas de la propia
        grilla se mantiene constante (ambas se corren para arriba en
        conjunto al scrollear). Pasado cierto scroll, la barra de pestañas
        queda geométricamente "más cerca" que la fila de arriba real, y
        Arriba salta directo a una pestaña (no necesariamente la activa,
        la que geométricamente cierre) en vez de subir una fila — pasaba
        en CUALQUIER fila suficientemente scrolleada, no solo cerca del
        principio. Indexar directo por (row, col) no depende de ninguna
        coordenada ni de cuánto esté scrolleado, así que no puede
        romperse de esta forma.

        (Comentario viejo, ya no aplica pero se deja como referencia: un
        override universal en todas las filas "probado antes" alternaba
        entre "ir a pestañas" y "subir una fila" de forma errática — esa
        versión vieja apuntaba a `_active_tab_button` en TODAS las filas,
        no al tile de arriba; apuntar al tile real de (row-1, col) no
        tiene ese problema.)"""
        overrides = {}
        if row == 0:
            overrides["up"] = self._active_tab_button
        else:
            overrides["up"] = lambda r=row, c=col: self._grid_tiles.get((r - 1, c))
        if col == 0:
            overrides["left"] = None
        if is_last_in_row:
            overrides["right"] = None
        return overrides

    def _on_search_changed(self, _text):
        self._render_grid()

    def _refresh_current_library(self):
        """Ver gallery.GalleryView._refresh_current_library — mismo
        diálogo bloqueante reusado tal cual (gallery.UpdateLibraryDialog,
        mismo criterio que ManageLibrariesDialog/AddLibraryDialog: TV
        nunca reimplementa un diálogo). Pedido explícito del usuario:
        bloquear la UI con progreso visible hasta que el re-escaneo Y el
        sondeo de pistas terminen, en vez de correr en silencio en
        background (ver CONTEXT.md Paso 33/35)."""
        if not self._current_library:
            return
        library = self._current_library
        dialog = gallery.UpdateLibraryDialog(library, self)
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

    def _switch_active_library(self, library):
        """Mismo criterio que gallery.GalleryView._switch_active_library
        (desktop, que ya lo tenía) — faltaba en TV. Clickear un resultado
        de búsqueda cross-library tiene que dejar la pestaña de SU
        biblioteca activa y la búsqueda limpia; si no, "Volver" desde la
        serie recién abierta caía en lo que fuera que estuviera activo
        ANTES de buscar (otra pestaña, o el buscador con texto todavía
        puesto), no en la biblioteca real de esa serie — bug real
        reportado en vivo."""
        self._current_library = library
        self._viewing_continue_watching = False
        self._current_items = self._items_cache.get(library.id, [])
        button = self._tab_buttons.get(library.id)
        if button:
            button.setChecked(True)
        self.search_input.blockSignals(True)
        self.search_input.clear()
        self.search_input.blockSignals(False)
        self._render_grid()

    def _on_item_clicked(self, library, subpath, name):
        # Mismo fix que gallery.GalleryView._on_item_clicked: cualquier
        # click con el buscador activo (misma biblioteca o distinta)
        # limpia el buscador y asienta la pestaña — antes solo pasaba si
        # la serie era de OTRA biblioteca.
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
        self._grid_tiles = {}

    # ---- "Continuar viendo" ----

    def refresh_continue_watching(self):
        """Público — para cuando los Pasos 3/4 (lista de episodios TV,
        reproductor TV) vuelvan acá y haya que reflejar progreso nuevo.
        Mismo patrón que GalleryView.refresh_continue_watching.

        Llamado por TVMainWindow._player_back al volver de una película:
        el tile "Continuar viendo" que tenía el foco antes de reproducir
        queda destruido acá abajo (_render_grid/_on_library_selected
        recrean todos los tiles desde cero) — a diferencia de
        TVEpisodeListView.reload(), que siempre re-enfoca explícitamente
        tras reconstruir sus filas, acá no había NINGÚN setFocus() después
        de reconstruir la grilla. Qt limpia el foco de la app cuando el
        widget enfocado se destruye, así que quedaba en None (bug real
        reportado en vivo: "cursor" invisible/inmóvil en modo TV al salir
        de una película desde "Continuar viendo", Tab lo mandaba al
        buscador por ser el próximo foco válido en el orden nativo de Qt).
        _reactivate_and_focus al final reproduce el mismo aterrizaje
        seguro que ya usan showEvent/_on_manage_dialog."""
        self._start_progress_refresh()
        self._update_continue_watching_tab_visibility()
        if not self._viewing_continue_watching:
            return
        if not self._continue_watching_button.isVisible():
            libraries = list(self._library_by_id.values())
            if libraries:
                self._tab_buttons[libraries[0].id].setChecked(True)
                self._on_library_selected(libraries[0])
            self._reactivate_and_focus(self._active_tab_button())
            return
        self._render_grid()
        self._reactivate_and_focus(self._active_tab_button())

    def _start_progress_refresh(self):
        if self._progress_refresh_worker is not None and self._progress_refresh_worker.isRunning():
            return
        libraries = list(self._library_by_id.values())
        if not any(getattr(lib, "progress_centralized", False) for lib in libraries):
            return
        self._progress_refresh_worker = gallery.ProgressRefreshWorker(libraries)
        self._progress_refresh_worker.refreshed.connect(self._on_progress_refreshed)
        self._progress_refresh_worker.start()

    def _on_progress_refreshed(self):
        self._update_continue_watching_tab_visibility()
        if self._viewing_continue_watching and self._continue_watching_button.isVisible():
            self._render_grid()

    def _has_resolvable_progress(self):
        return gallery.has_resolvable_progress(self._library_by_id)

    def _update_continue_watching_tab_visibility(self):
        has_progress = self._has_resolvable_progress()
        self._continue_watching_button.setVisible(has_progress)
        self._rewire_top_row_navigation()
        return has_progress

    def _on_continue_watching_tab_selected(self):
        self._current_library = None
        self._viewing_continue_watching = True
        self._render_grid()

    def _resolve_continue_watching_entries(self):
        return gallery.resolve_continue_watching_entries(self._library_by_id, self._items_cache)

    def update_series_name(self, library, subpath, new_name):
        """Conectado a TVEpisodeListView.series_renamed (ver tv_episodes.py)
        — TV no permite renombrar, pero el nombre resuelto (custom o no)
        puede diferir del que ya está cacheado acá, así que igual hace
        falta parchearlo para que la galería lo muestre bien al volver."""
        if gallery.patch_series_name(self._items_cache, library, subpath, new_name):
            self._render_grid()

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
            tile = TVPosterTile(
                entry["name"], pixmap, progress_fraction=entry["fraction"],
                on_click=lambda e=entry: self._on_continue_watching_clicked(e),
            )
            row, col = divmod(i, TV_GRID_COLUMNS)
            is_last_in_row = col == TV_GRID_COLUMNS - 1 or i == len(entries) - 1
            tile._tv_focus_override = self._poster_focus_override(row, col, is_last_in_row)
            self.grid_layout.addWidget(tile, row, col)
            self._grid_tiles[(row, col)] = tile

    def _on_continue_watching_clicked(self, entry):
        library = entry["library"]
        if entry["is_movie"]:
            self.play_requested.emit(library, entry["episode_subpath"], entry["name"])
        else:
            self.resume_episode_requested.emit(
                library, entry["series_subpath"], entry["episode_subpath"], entry["name"]
            )
