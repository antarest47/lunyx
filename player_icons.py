"""
Íconos del reproductor, dibujados a mano (QPainter) en vez de glifos
Unicode/emoji — compartido entre main.IconButton (escritorio) y
tv_player.TVIconButton, para que los símbolos sean IDÉNTICOS entre las
dos vistas (pedido explícito del usuario). Los glifos de distintos
símbolos (▶ vs ⏸, por ejemplo) tienen métricas de tamaño MUY
inconsistentes entre sí en la fuente del sistema — dibujar a mano
garantiza que todos midan exactamente lo mismo, sea cual sea el tamaño
real del botón (todo escala proporcional a `s`).

Sin ningún widget acá — cada vista tiene su PROPIA clase de botón (con su
propio focus policy/estilo/tamaño), que llama a paint_icon() desde su
paintEvent(). Mismo criterio que player_engine.py: lógica pura
compartida, nunca un widget compartido."""

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QPainterPath, QPen


def _draw_play(p, cx, cy, s, color):
    path = QPainterPath()
    path.moveTo(cx - s * 1.3, cy - s * 1.6)
    path.lineTo(cx - s * 1.3, cy + s * 1.6)
    path.lineTo(cx + s * 1.6, cy)
    path.closeSubpath()
    p.drawPath(path)


def _draw_pause(p, cx, cy, s, color):
    bar_w, bar_h, gap = s * 1.1, s * 3.2, s * 0.7
    p.drawRoundedRect(QRectF(cx - gap - bar_w, cy - bar_h / 2, bar_w, bar_h), 1.5, 1.5)
    p.drawRoundedRect(QRectF(cx + gap, cy - bar_h / 2, bar_w, bar_h), 1.5, 1.5)


def _draw_seek_forward(p, cx, cy, s, color):
    gap = s * 1.5
    for ox in (cx - gap, cx + gap):
        path = QPainterPath()
        path.moveTo(ox - s * 0.9, cy - s * 1.4)
        path.lineTo(ox - s * 0.9, cy + s * 1.4)
        path.lineTo(ox + s * 0.9, cy)
        path.closeSubpath()
        p.drawPath(path)


def _draw_seek_back(p, cx, cy, s, color):
    gap = s * 1.5
    for ox in (cx - gap, cx + gap):
        path = QPainterPath()
        path.moveTo(ox + s * 0.9, cy - s * 1.4)
        path.lineTo(ox + s * 0.9, cy + s * 1.4)
        path.lineTo(ox - s * 0.9, cy)
        path.closeSubpath()
        p.drawPath(path)


def _draw_prev(p, cx, cy, s, color):
    p.drawRoundedRect(QRectF(cx - s * 1.9, cy - s * 1.6, s * 0.9, s * 3.2), 1, 1)
    path = QPainterPath()
    path.moveTo(cx + s * 1.7, cy - s * 1.6)
    path.lineTo(cx + s * 1.7, cy + s * 1.6)
    path.lineTo(cx - s * 0.5, cy)
    path.closeSubpath()
    p.drawPath(path)


def _draw_next(p, cx, cy, s, color):
    path = QPainterPath()
    path.moveTo(cx - s * 1.7, cy - s * 1.6)
    path.lineTo(cx - s * 1.7, cy + s * 1.6)
    path.lineTo(cx + s * 0.5, cy)
    path.closeSubpath()
    p.drawPath(path)
    p.drawRoundedRect(QRectF(cx + s * 1.0, cy - s * 1.6, s * 0.9, s * 3.2), 1, 1)


def _draw_speaker(p, cx, cy, s, color):
    path = QPainterPath()
    path.moveTo(cx - s * 2.2, cy - s * 0.8)
    path.lineTo(cx - s * 1.0, cy - s * 0.8)
    path.lineTo(cx + s * 0.4, cy - s * 1.9)
    path.lineTo(cx + s * 0.4, cy + s * 1.9)
    path.lineTo(cx - s * 1.0, cy + s * 0.8)
    path.lineTo(cx - s * 2.2, cy + s * 0.8)
    path.closeSubpath()
    p.drawPath(path)


def _draw_volume(p, cx, cy, s, color):
    _draw_speaker(p, cx, cy, s, color)
    pen = QPen(color, max(1.4, s * 0.35))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    for radius in (s * 1.0, s * 1.7):
        rect = QRectF(cx + s * 0.3 - radius, cy - radius, radius * 2, radius * 2)
        p.drawArc(rect, -35 * 16, 70 * 16)  # ángulos de QPainter en 1/16 de grado


def _draw_volume_muted(p, cx, cy, s, color):
    _draw_speaker(p, cx, cy, s, color)
    pen = QPen(color, max(1.6, s * 0.4))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    p.drawLine(QPointF(cx + s * 0.8, cy - s * 1.1), QPointF(cx + s * 2.2, cy + s * 1.1))
    p.drawLine(QPointF(cx + s * 2.2, cy - s * 1.1), QPointF(cx + s * 0.8, cy + s * 1.1))


def _draw_fullscreen(p, cx, cy, s, color):
    pen = QPen(color, max(1.6, s * 0.45))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    half = s * 2.0
    arm = s * 1.1
    for sx, sy in ((-1, -1), (1, -1), (-1, 1), (1, 1)):
        x, y = cx + sx * half, cy + sy * half
        p.drawLine(QPointF(x, y), QPointF(x - sx * arm, y))
        p.drawLine(QPointF(x, y), QPointF(x, y - sy * arm))


def _draw_back(p, cx, cy, s, color):
    pen = QPen(color, max(1.6, s * 0.45))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    path = QPainterPath()
    path.moveTo(cx + s * 1.4, cy - s * 1.8)
    path.lineTo(cx - s * 1.4, cy)
    path.lineTo(cx + s * 1.4, cy + s * 1.8)
    p.drawPath(path)


_DRAWERS = {
    "play": _draw_play,
    "pause": _draw_pause,
    "seek_forward": _draw_seek_forward,
    "seek_back": _draw_seek_back,
    "prev": _draw_prev,
    "next": _draw_next,
    "volume": _draw_volume,
    "volume_muted": _draw_volume_muted,
    "fullscreen": _draw_fullscreen,
    "back": _draw_back,
}


def paint_icon(painter, icon_name, cx, cy, s, color):
    draw = _DRAWERS.get(icon_name)
    if draw:
        draw(painter, cx, cy, s, color)
