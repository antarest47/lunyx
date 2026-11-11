"""
Navegación por flechas para el modo TV — genérica, no depende de ninguna
vista puntual.

Dos mecanismos, para dos formas de contenido:
- install_arrow_navigation(): navegación LINEAL por tab-order
  (focusNextChild()/focusPreviousChild(), ya provistos por Qt). Pensada
  para diálogos existentes que ya se navegan bien con Tab/Shift+Tab
  (ManageLibrariesDialog, AddLibraryDialog) — así el mismo diálogo sirve
  para desktop (mouse/Tab) y TV (flechas) sin tocarlo.
- install_spatial_navigation(): navegación por GEOMETRÍA real de los
  widgets — necesaria para una grilla 2D (ej. la grilla de posters de
  TVGalleryView), donde tab-order lineal haría que Up/Down se comporten
  igual que Left/Right (rompe la expectativa de que Down baje una fila).

Escape no se intercepta en ninguno de los dos — Qt ya lo maneja solo
(dispara reject() en un QDialog). Enter/Return SÍ se intercepta en
install_arrow_navigation (ver _ArrowNavigationFilter): en teoría
QPushButton.keyPressEvent ya lo maneja solo si autoDefault/isDefault
está activo, y así se confirmó en un test aislado — pero un usuario
real reportó que en la app real (navegando con flechas hasta un botón
tipo "Editar"/"Agregar biblioteca nueva") Espacio activaba el botón y
Enter no, un caso que no se pudo reproducir en un test automatizado
pese a varios intentos. En vez de seguir persiguiendo la causa exacta,
se agregó un manejo explícito (si el foco está en un QAbstractButton,
Enter/Return/Select lo clickea directo) que no depende de qué mecanismo
interno de Qt esté (o no) activándolo — mismo criterio que
TVPosterTile/TVEpisodeRow/TVValuePicker, que tampoco confían en el
comportamiento por defecto de Qt para esto.

**Dos detalles no obvios que costaron bugs reales en vivo**:

1. Los dos filtros se instalan en QApplication, NO en el widget/diálogo
   en sí. Un evento de teclado real se entrega directo al widget que
   tiene el foco (un campo del formulario, un poster de la grilla) — un
   event filter instalado en un ANCESTRO (el diálogo, la vista
   contenedora) nunca ve esos eventos, porque Qt no los hace "burbujear"
   a los ancestros salvo casos especiales que no aplican acá. Instalarlo
   en QApplication sí los intercepta a todos, desde cualquier
   profundidad.

2. El guard de "¿el foco está realmente adentro de este container?" NO
   puede usar `container.focusWidget()` — ese método reporta el ÚLTIMO
   widget que tuvo foco adentro del container, aunque el container ya
   esté oculto (ej. un QDialog que se cerró pero sigue vivo como hijo Qt
   de quien lo abrió, nunca destruido mientras esa referencia exista).
   Eso hacía que el filtro de un diálogo YA CERRADO siguiera
   "tragándose" las flechas (devolvía True, `focusPreviousChild()` sin
   efecto visible porque el diálogo está oculto, pero el evento nunca
   llegaba al filtro de la vista de atrás) — síntoma real: "las flechas
   dejan de responder después de cerrar Gestionar bibliotecas". El fix:
   comparar contra `QApplication.focusWidget()` (el foco GLOBAL real),
   preguntando si ESE widget está adentro de este container — así un
   container oculto/inactivo correctamente no actúa, sin importar qué
   crea recordar sobre su propio último foco.

Con el guard correcto, varios containers conviven con su propio filtro
activo a la vez sin pisarse (Qt llama a los filtros del más reciente al
más viejo, y corta en el primero que devuelve True).
"""

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import QAbstractButton, QAbstractScrollArea, QApplication, QLineEdit, QWidget

# Ver TV_OWNS_HORIZONTAL_NAV_ATTR más abajo para el porqué. QLineEdit se
# chequea con isinstance (no con el atributo marcador) porque CUALQUIER
# campo de texto necesita Izquierda/Derecha para mover el cursor — no
# tiene sentido pedirle a cada QLineEdit de la app que se marque a mano.
def _owns_horizontal_nav(focused):
    return isinstance(focused, QLineEdit) or getattr(focused, TV_OWNS_HORIZONTAL_NAV_ATTR, False)


def _focus_is_inside(container):
    """True si el widget con foco REAL (global, QApplication.focusWidget())
    está adentro de `container` — a diferencia de container.focusWidget(),
    que puede reportar un foco "recordado" viejo aunque container esté
    oculto/inactivo (ver nota del módulo, punto 2)."""
    focused = QApplication.focusWidget()
    if focused is None:
        return False
    return focused is container or container.isAncestorOf(focused)


class _ArrowNavigationFilter(QObject):
    def __init__(self, container):
        super().__init__(container)
        self._container = container

    def eventFilter(self, watched, event):
        key = event.key() if event.type() in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease) else None
        if key is None:
            return False
        if not _focus_is_inside(self._container):
            return False

        # Enter/Return/Select: consumir el ciclo COMPLETO (press Y
        # release), disparando el click recién en el release — igual que
        # Qt hace nativo con un botón enfocado (press = estado "hundido"
        # visual, release = click() real). Bug real reportado en vivo con
        # el primer intento (click() en el PRESS): si el botón abre un
        # diálogo nuevo (ej. "Editar"/"+ Agregar biblioteca nueva" en
        # ManageLibrariesDialog), ese diálogo queda mostrado ANTES de que
        # el usuario suelte la tecla — el release, que nunca se consumió
        # acá, le llegaba entonces a lo que tuviera foco en el diálogo
        # NUEVO (ej. su botón "Guardar"), disparando una acción no
        # pedida ahí (guardado prematuro con campos vacíos, o cierre
        # inmediato). Consumir las DOS mitades del ciclo en el mismo
        # widget evita que sobre ninguna a mitad de camino.
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Select):
            if event.type() == QEvent.Type.KeyRelease:
                focused = QApplication.focusWidget()
                if isinstance(focused, QAbstractButton) and focused.isEnabled():
                    focused.click()
            return True

        if event.type() != QEvent.Type.KeyPress:
            return False
        if key in (Qt.Key.Key_Left, Qt.Key.Key_Right) and _owns_horizontal_nav(QApplication.focusWidget()):
            return False
        if key in (Qt.Key.Key_Down, Qt.Key.Key_Right):
            self._container.focusNextChild()
            return True
        if key in (Qt.Key.Key_Up, Qt.Key.Key_Left):
            self._container.focusPreviousChild()
            return True
        return False


def install_arrow_navigation(widget):
    """Instala la traducción de flechas en `widget` (típicamente un
    QDialog a punto de mostrarse con exec()). El filtro se guarda como
    atributo del propio widget porque Qt no retiene una referencia fuerte
    a un QObject pasado a installEventFilter() — sin esto, Python lo
    recolectaría apenas termine esta función y el filtro dejaría de
    funcionar. Al pasarle `widget` como padre Qt del filtro, además, se
    des-registra solo de QApplication cuando `widget` se destruye (ej. el
    diálogo se cierra) — no hace falta desinstalarlo a mano."""
    nav_filter = _ArrowNavigationFilter(widget)
    QApplication.instance().installEventFilter(nav_filter)
    widget._tv_nav_filter = nav_filter


# Atributo marcador (ver TV_OWNS_HORIZONTAL_NAV_ATTR más abajo) — un
# widget que lo tenga en True le "avisa" a _SpatialNavigationFilter que
# quiere manejar Izquierda/Derecha él mismo (ej. un selector de valor tipo
# "‹ Español ›" que cicla opciones con las flechas, ver TVValuePicker en
# tv_episodes.py). Hace falta este mecanismo porque un QComboBox real no
# funciona acá: su popup recibe las flechas para navegar la lista
# desplegada, pero _SpatialNavigationFilter está instalado en
# QApplication y las intercepta ANTES de que lleguen al popup — Arriba/
# Abajo NO se excluyen a propósito, así el widget sigue siendo alcanzable
# navegando verticalmente como cualquier otro.
TV_OWNS_HORIZONTAL_NAV_ATTR = "_tv_owns_horizontal_nav"


class _SpatialNavigationFilter(QObject):
    def __init__(self, container):
        super().__init__(container)
        self._container = container

    def eventFilter(self, watched, event):
        if event.type() != QEvent.Type.KeyPress:
            return False
        direction = {
            Qt.Key.Key_Up: "up", Qt.Key.Key_Down: "down",
            Qt.Key.Key_Left: "left", Qt.Key.Key_Right: "right",
        }.get(event.key())
        if direction is None:
            return False
        if not _focus_is_inside(self._container):
            return False
        if direction in ("left", "right") and _owns_horizontal_nav(QApplication.focusWidget()):
            return False
        _move_focus(self._container, direction)
        return True


# Atributo marcador: un widget puede fijar
# `widget._tv_focus_override = {"up": destino, "right": destino, ...}`
# (destino = un widget, o un callable sin argumentos que devuelve un
# widget o None, resuelto recién al navegar — así puede reflejar estado
# que cambia con el tiempo, ej. "la pestaña actualmente activa") para
# forzar el destino de una dirección puntual, saltándose la búsqueda
# geométrica de más abajo. Pensado para casos donde la geometría cruda da
# un resultado técnicamente "más cercano" pero semánticamente confuso —
# ej. TVGalleryView: desde CUALQUIER columna de la grilla de posters,
# Arriba tiene que ir siempre a la pestaña activa (no a la que
# geométricamente quede más alineada, que además podría ser el buscador
# si la pestaña activa es angosta); o encadenar tabs+botón de gestión
# como una sola fila para Izquierda/Derecha, sin que el buscador (que
# vive en otra fila, pero puede terminar más "cerca" en x) se interponga.
# Si la dirección está en el mapa, el override manda siempre — incluso si
# resuelve a None (se interpreta como "no mover el foco en esa
# dirección", un bloqueo explícito, no una señal para caer al cálculo
# geométrico).
TV_FOCUS_OVERRIDE_ATTR = "_tv_focus_override"


def _move_focus(container, direction):
    """Heurística estándar de navegación direccional (misma idea que usa
    CSS spatial navigation / el focus search de Android): entre los
    widgets que están genuinamente en la dirección pedida (coordenada
    primaria estrictamente mayor que la del foco actual), elige el más
    cercano, penalizando el desalineamiento en el eje cruzado — así Down
    prefiere el widget de abajo más alineado horizontalmente, no
    cualquiera que esté simplemente "más abajo". Funciona para cualquier
    layout (fila de tabs + grilla + botón suelto) sin que el caller tenga
    que enseñarle "esto es una grilla de N columnas": la geometría real
    ya lo resuelve. Excepción: TV_FOCUS_OVERRIDE_ATTR, ver arriba."""
    current = QApplication.focusWidget()  # NO container.focusWidget(), ver nota del módulo
    if current is None:
        return

    overrides = getattr(current, TV_FOCUS_OVERRIDE_ATTR, None)
    if overrides and direction in overrides:
        target = overrides[direction]
        target = target() if callable(target) else target
        if target is not None:
            target.setFocus()
        return

    current_center = current.mapTo(container, current.rect().center())

    def axis_score(center):
        dx = center.x() - current_center.x()
        dy = center.y() - current_center.y()
        primary = {"up": -dy, "down": dy, "left": -dx, "right": dx}[direction]
        cross = abs(dx) if direction in ("up", "down") else abs(dy)
        return primary, cross

    candidates = []
    for w in container.findChildren(QWidget):
        if w is current or w.focusPolicy() == Qt.FocusPolicy.NoFocus or not w.isVisible():
            continue
        # QAbstractScrollArea (QScrollArea acá, siempre usado como wrapper
        # estructural de contenido scrolleable, nunca como una parada de
        # navegación en sí) tiene foco aceptable por default en Qt (así
        # PageUp/Down funcionan si nada adentro tiene foco) — sin excluirlo
        # acá, es un candidato geométrico más, y su "centro" es el de TODO
        # el viewport visible, que en una vista con contenido scrolleable
        # (ej. TVEpisodeListView) suele terminar más cerca del foco actual
        # que el destino semánticamente correcto (bug real reportado en
        # vivo: Arriba desde el primer episodio aterrizaba en el
        # scroll_area en vez de en "Subtítulos por defecto" — sin ningún
        # QSS de :focus para QScrollArea, la selección visualmente
        # desaparecía por completo, "como si hubiera algo en medio").
        if isinstance(w, QAbstractScrollArea):
            continue
        center = w.mapTo(container, w.rect().center())
        primary, cross = axis_score(center)
        if primary > 0:
            candidates.append((w, primary, cross))

    if not candidates:
        return
    best = min(candidates, key=lambda item: item[1] + item[2] * 2)
    best[0].setFocus()


def install_spatial_navigation(container):
    """Instala navegación espacial en `container` (típicamente una vista
    completa como TVGalleryView, no un diálogo chico). Mismo motivo que
    install_arrow_navigation() para guardar la referencia al filtro."""
    nav_filter = _SpatialNavigationFilter(container)
    QApplication.instance().installEventFilter(nav_filter)
    container._tv_nav_filter = nav_filter


class _EscapeFilter(QObject):
    def __init__(self, container, callback):
        super().__init__(container)
        self._container = container
        self._callback = callback

    def eventFilter(self, watched, event):
        if event.type() != QEvent.Type.KeyPress:
            return False
        if event.key() != Qt.Key.Key_Escape:
            return False
        if not _focus_is_inside(self._container):
            return False
        focused = QApplication.focusWidget()
        if isinstance(focused, QLineEdit) and focused.text():
            # Bug real reportado en vivo: cuando el SearchLineEdit vive
            # ADENTRO del mismo container que instala este filtro (caso de
            # TVGalleryView, buscador propio), _focus_is_inside() da True
            # para el buscador igual que para cualquier otro hijo — sin
            # este chequeo, el callback de "volver" se comía el Escape
            # ANTES de que le llegara al keyPressEvent propio de
            # SearchLineEdit (ver gallery.SearchLineEdit), y el buscador
            # nunca se limpiaba. Dejar pasar el evento (return False) solo
            # cuando hay texto que limpiar reproduce el comportamiento de
            # escritorio; con el campo vacío, Escape sigue haciendo "volver"
            # como siempre.
            return False
        self._callback()
        return True


def install_escape_back(container, callback):
    """Escape = "volver" en `container` (dispara `callback`, sin
    argumentos) cuando el foco está adentro. Mismo motivo que las otras
    dos funciones de este módulo para instalarlo en QApplication en vez
    del propio `container`: un evento de teclado real se entrega directo
    al widget con foco, no burbujea solo a los ancestros (ver nota del
    módulo, punto 1) — un keyPressEvent en `container` nunca vería el
    Escape si el foco está en un hijo. El guard de _focus_is_inside evita
    pisar el Escape-para-limpiar de un SearchLineEdit en un `container`
    DISTINTO al que instala este filtro (el evento sigue de largo sin que
    este filtro lo toque); para un SearchLineEdit que vive ADENTRO del
    mismo container (caso de TVGalleryView) hace falta además el chequeo
    explícito de arriba (QLineEdit con texto)."""
    escape_filter = _EscapeFilter(container, callback)
    QApplication.instance().installEventFilter(escape_filter)
    container._tv_escape_filter = escape_filter
