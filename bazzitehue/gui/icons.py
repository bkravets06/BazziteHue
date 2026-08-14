"""Icons drawn at runtime with QPainter.

Drawing them rather than shipping bitmaps keeps the package free of binary
assets and lets the tray icon take on the colour the lamp is currently showing.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QIcon,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
)

OFF_COLOR = QColor(120, 124, 130)


def _bulb_path(rect: QRectF) -> tuple[QPainterPath, QPainterPath]:
    """Return (glass, base) paths for a bulb drawn inside ``rect``."""
    width = rect.width()
    height = rect.height()

    glass = QPainterPath()
    glass_size = width * 0.62
    glass_rect = QRectF(
        rect.left() + (width - glass_size) / 2,
        rect.top() + height * 0.06,
        glass_size,
        glass_size,
    )
    glass.addEllipse(glass_rect)

    # The neck: a soft wedge from the glass down to the screw base.
    neck = QPainterPath()
    neck.moveTo(glass_rect.left() + glass_size * 0.22, glass_rect.bottom() - glass_size * 0.12)
    neck.lineTo(glass_rect.right() - glass_size * 0.22, glass_rect.bottom() - glass_size * 0.12)
    neck.lineTo(glass_rect.right() - glass_size * 0.30, glass_rect.bottom() + height * 0.12)
    neck.lineTo(glass_rect.left() + glass_size * 0.30, glass_rect.bottom() + height * 0.12)
    neck.closeSubpath()
    glass = glass.united(neck)

    base = QPainterPath()
    base_width = glass_size * 0.44
    base.addRoundedRect(
        QRectF(
            rect.center().x() - base_width / 2,
            glass_rect.bottom() + height * 0.10,
            base_width,
            height * 0.18,
        ),
        width * 0.04,
        width * 0.04,
    )
    return glass, base


def bulb_pixmap(color: QColor | None, size: int = 64, on: bool = True) -> QPixmap:
    """A lightbulb tinted with ``color`` (grey and hollow when off)."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)

    rect = QRectF(size * 0.12, size * 0.06, size * 0.76, size * 0.82)
    glass, base = _bulb_path(rect)
    tint = QColor(color) if (color is not None and on) else QColor(OFF_COLOR)

    if on:
        # A halo so the icon still reads as "lit" against a dark panel.
        halo = QRadialGradient(rect.center(), size * 0.5)
        glow = QColor(tint)
        glow.setAlpha(110)
        halo.setColorAt(0.0, glow)
        glow = QColor(tint)
        glow.setAlpha(0)
        halo.setColorAt(1.0, glow)
        painter.setPen(Qt.NoPen)
        painter.setBrush(halo)
        painter.drawEllipse(rect.adjusted(-size * 0.08, -size * 0.08, size * 0.08, size * 0.08))

        fill = QLinearGradient(rect.topLeft(), rect.bottomRight())
        fill.setColorAt(0.0, tint.lighter(125))
        fill.setColorAt(1.0, tint.darker(115))
        painter.setBrush(fill)
    else:
        painter.setBrush(Qt.NoBrush)

    pen = QPen(tint.darker(160) if on else tint, max(1.0, size * 0.05))
    pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(pen)
    painter.drawPath(glass)

    painter.setBrush(tint.darker(150) if on else Qt.NoBrush)
    painter.drawPath(base)
    painter.end()
    return pixmap


def bulb_icon(color: QColor | None, size: int = 64, on: bool = True) -> QIcon:
    return QIcon(bulb_pixmap(color, size=size, on=on))


def app_icon() -> QIcon:
    """The window/application icon: a warm lit bulb at several sizes."""
    icon = QIcon()
    warm = QColor(255, 176, 74)
    for size in (16, 22, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(bulb_pixmap(warm, size=size, on=True))
    return icon


def swatch_pixmap(color: QColor, size: int = 24) -> QPixmap:
    """A rounded colour chip used on preset buttons."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(QPen(QColor(0, 0, 0, 60), 1))
    painter.setBrush(color)
    painter.drawRoundedRect(QRectF(1, 1, size - 2, size - 2), size * 0.25, size * 0.25)
    painter.end()
    return pixmap


def marker(painter: QPainter, point: QPointF, radius: float) -> None:
    """The ring that marks the selected point on the colour wheel."""
    painter.setBrush(Qt.NoBrush)
    painter.setPen(QPen(QColor(0, 0, 0, 160), 3))
    painter.drawEllipse(point, radius, radius)
    painter.setPen(QPen(QColor(255, 255, 255, 230), 2))
    painter.drawEllipse(point, radius, radius)
