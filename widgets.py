"""
Widgets chicos reusados en más de una vista (episodes.py, main.py). Lo
que solo se usa en una vista se queda definido ahí mismo — esto es
únicamente para lo que se repite en más de un lugar.
"""

import math

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QPushButton

import theme


def _rounded_polygon_path(vertices):
    """vertices: [(QPointF, radio), ...], en orden (sentido horario o
    antihorario, da igual). Devuelve un QPainterPath cerrado con cada
    vértice redondeado a su propio radio: en vez de llegar en punta a
    cada vértice, la línea se corta ANTES (retrocediendo `radio` a lo
    largo de cada arista que entra/sale de ese vértice) y esos dos
    puntos se unen con una curva (quadTo, usando el vértice original
    como punto de control) — la forma estándar de "esquina redondeada"
    en un polígono arbitrario, no solo en un rectángulo."""

    def _point_toward(origin, target, distance):
        dx, dy = target.x() - origin.x(), target.y() - origin.y()
        length = math.hypot(dx, dy)
        if length == 0:
            return QPointF(origin)
        distance = min(distance, length / 2)
        return QPointF(origin.x() + dx / length * distance, origin.y() + dy / length * distance)

    n = len(vertices)

    def entry_corner_exit(i):
        prev_pt = vertices[(i - 1) % n][0]
        corner, radius = vertices[i]
        next_pt = vertices[(i + 1) % n][0]
        entry = _point_toward(corner, prev_pt, radius)
        exit_ = _point_toward(corner, next_pt, radius)
        return entry, corner, exit_

    path = QPainterPath()
    first_entry, _, _ = entry_corner_exit(0)
    path.moveTo(first_entry)
    for i in range(n):
        entry, corner, exit_ = entry_corner_exit(i)
        if i != 0:
            path.lineTo(entry)
        path.quadTo(corner, exit_)
    path.closeSubpath()
    return path


class BackButton(QPushButton):
    """Botón "Volver" con forma de flecha en vez de un "<" como prefijo
    de texto: un rectángulo con esquinas redondeadas normales del lado
    derecho, pero el lado izquierdo estirado en una punta larga (lados
    rectos, no una curva) con las esquinas de esa punta también
    redondeadas — un pentágono apuntando a la izquierda, tipo lápiz
    acostado. Pintado a mano con QPainterPath porque QSS no puede
    expresar un borde que siga un contorno no rectangular (el borde
    ámbar tiene que rodear la punta también, no solo los lados rectos).

    Todas las medidas son proporcionales al ALTO del botón (no píxeles
    fijos) para que la forma se vea bien sea cual sea el tamaño real que
    le termine dando el layout."""

    CORNER_RADIUS_RATIO = 0.30  # esquinas de arriba/abajo-derecha
    SHOULDER_RADIUS_RATIO = 0.14  # donde el borde recto de arriba/abajo se encuentra con la diagonal de la punta
    TIP_RADIUS_RATIO = 0.16  # la punta en sí
    TIP_DEPTH_RATIO = 0.95  # qué tan larga es la punta (horizontal), relativa al alto

    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        self.setFlat(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        # Sin fondo/borde propios de QPushButton — todo lo dibuja
        # paintEvent() a mano, incluido el texto.
        self.setStyleSheet("QPushButton { background: transparent; border: none; }")

    def sizeHint(self):
        base = super().sizeHint()
        tip_depth = base.height() * self.TIP_DEPTH_RATIO
        # Mismo alto que un QPushButton normal (ej. "Marcar serie como
        # vista") — no se le suma nada acá, a propósito, para que las dos
        # queden a la misma altura en la misma fila.
        return QSize(int(base.width() + tip_depth + 10), base.height())

    def _build_path(self):
        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)  # margen para que el trazo del borde no se recorte
        h = rect.height()
        r = h * self.CORNER_RADIUS_RATIO
        shoulder_r = h * self.SHOULDER_RADIUS_RATIO
        tip_r = h * self.TIP_RADIUS_RATIO
        tip_depth = h * self.TIP_DEPTH_RATIO
        cy = rect.center().y()

        vertices = [
            (QPointF(rect.left(), cy), tip_r),                      # la punta
            (QPointF(rect.left() + tip_depth, rect.top()), shoulder_r),     # "hombro" arriba
            (QPointF(rect.right(), rect.top()), r),                        # esquina arriba-derecha
            (QPointF(rect.right(), rect.bottom()), r),                     # esquina abajo-derecha
            (QPointF(rect.left() + tip_depth, rect.bottom()), shoulder_r),  # "hombro" abajo
        ]
        return _rounded_polygon_path(vertices)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        is_active = self.underMouse() or self.isDown()
        path = self._build_path()
        painter.setBrush(QColor(theme.SURFACE_HOVER if is_active else theme.SURFACE))
        pen = QPen(QColor(theme.ACCENT_HOVER if is_active else theme.ACCENT))
        pen.setWidthF(1.4)
        painter.setPen(pen)
        painter.drawPath(path)

        painter.setPen(QColor(theme.ACCENT_HOVER if is_active else theme.ACCENT))
        tip_depth = self.rect().height() * self.TIP_DEPTH_RATIO
        text_rect = QRectF(self.rect()).adjusted(tip_depth + 8, 0, -8, 0)
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.text())

    def enterEvent(self, event):
        super().enterEvent(event)
        self.update()

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self.update()
