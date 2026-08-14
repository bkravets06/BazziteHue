"""Custom widgets: the colour wheel and the white-temperature bar."""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QConicalGradient,
    QLinearGradient,
    QPainter,
    QPen,
    QPixmap,
    QRadialGradient,
)
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..color import kelvin_to_xy, xy_to_rgb
from .icons import marker


def wheel_point(rect: QRectF, hue: float, saturation: float) -> QPointF:
    """Where a hue/saturation pair sits on the wheel.

    QConicalGradient runs counter-clockwise from its start angle, so hue 0 is at
    the top and hue increases counter-clockwise. Screen y grows downwards, hence
    the negated sine.
    """
    radius = rect.width() / 2
    angle = math.radians(90 + hue)
    distance = radius * (max(0.0, min(100.0, saturation)) / 100)
    return QPointF(
        rect.center().x() + math.cos(angle) * distance,
        rect.center().y() - math.sin(angle) * distance,
    )


def wheel_color_at(rect: QRectF, point: QPointF) -> tuple[float, float]:
    """The hue and saturation under a point -- the inverse of wheel_point."""
    radius = rect.width() / 2
    if radius <= 0:
        return (0.0, 0.0)

    dx = point.x() - rect.center().x()
    dy = point.y() - rect.center().y()
    hue = (math.degrees(math.atan2(-dy, dx)) - 90) % 360
    saturation = min(1.0, math.hypot(dx, dy) / radius) * 100
    return (hue, saturation)


class ColorWheel(QWidget):
    """An HSV wheel: hue around the circumference, saturation towards the edge.

    Value is deliberately not part of it -- brightness is a separate control on
    the lamp, and folding it in here would make the same colour reachable from
    two places that disagree.
    """

    #: hue in degrees, saturation as a percentage
    color_picked = Signal(float, float)
    #: emitted when the user lets go, for callers that want to commit once
    picking_finished = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._hue = 30.0
        self._saturation = 100.0
        self._wheel: QPixmap | None = None
        self.setMinimumSize(180, 180)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setCursor(Qt.CrossCursor)
        self.setFocusPolicy(Qt.StrongFocus)

    # -- state -------------------------------------------------------- #

    def hue(self) -> float:
        return self._hue

    def saturation(self) -> float:
        return self._saturation

    def set_hsv(self, hue: float, saturation: float) -> None:
        self._hue = hue % 360
        self._saturation = max(0.0, min(100.0, saturation))
        self.update()

    # -- painting ----------------------------------------------------- #

    def _wheel_rect(self) -> QRectF:
        side = min(self.width(), self.height()) - 12
        return QRectF(
            (self.width() - side) / 2,
            (self.height() - side) / 2,
            side,
            side,
        )

    def _render_wheel(self, rect: QRectF) -> QPixmap:
        ratio = self.devicePixelRatioF()
        pixmap = QPixmap(int(rect.width() * ratio), int(rect.height() * ratio))
        pixmap.setDevicePixelRatio(ratio)
        pixmap.fill(Qt.transparent)

        local = QRectF(0, 0, rect.width(), rect.height())
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)

        hues = QConicalGradient(local.center(), 90)
        for step in range(0, 361, 15):
            hues.setColorAt(step / 360, QColor.fromHsvF((step % 360) / 360, 1.0, 1.0))
        painter.setPen(Qt.NoPen)
        painter.setBrush(hues)
        painter.drawEllipse(local)

        # White in the middle fades the saturation out towards the centre.
        whites = QRadialGradient(local.center(), local.width() / 2)
        whites.setColorAt(0.0, QColor(255, 255, 255, 255))
        whites.setColorAt(1.0, QColor(255, 255, 255, 0))
        painter.setBrush(whites)
        painter.drawEllipse(local)
        painter.end()
        return pixmap

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self._wheel = None
        super().resizeEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        rect = self._wheel_rect()
        if rect.width() <= 0:
            return

        if self._wheel is None:
            self._wheel = self._render_wheel(rect)

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.drawPixmap(rect.topLeft(), self._wheel)

        painter.setPen(QPen(QColor(0, 0, 0, 40), 1))
        painter.setBrush(Qt.NoBrush)
        painter.drawEllipse(rect)

        radius = rect.width() / 2
        point = wheel_point(rect, self._hue, self._saturation)
        marker(painter, point, max(5.0, radius * 0.07))
        painter.end()

    # -- interaction --------------------------------------------------- #

    def _pick(self, position: QPointF) -> None:
        rect = self._wheel_rect()
        if rect.width() <= 0:
            return

        hue, saturation = wheel_color_at(rect, position)
        self.set_hsv(hue, saturation)
        self.color_picked.emit(self._hue, self._saturation)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self._pick(event.position())

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.buttons() & Qt.LeftButton:
            self._pick(event.position())

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self.picking_finished.emit()

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        """Arrow keys move around the wheel, so it works without a mouse."""
        step = 2.0 if event.modifiers() & Qt.ShiftModifier else 10.0
        if event.key() == Qt.Key_Left:
            self.set_hsv(self._hue - step, self._saturation)
        elif event.key() == Qt.Key_Right:
            self.set_hsv(self._hue + step, self._saturation)
        elif event.key() == Qt.Key_Up:
            self.set_hsv(self._hue, self._saturation + step)
        elif event.key() == Qt.Key_Down:
            self.set_hsv(self._hue, self._saturation - step)
        else:
            super().keyPressEvent(event)
            return
        self.color_picked.emit(self._hue, self._saturation)
        self.picking_finished.emit()


class TemperatureBar(QWidget):
    """The warm-to-cool ramp shown above the colour temperature slider."""

    def __init__(self, minimum: int, maximum: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.minimum = minimum
        self.maximum = maximum
        self.setFixedHeight(18)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        gradient = QLinearGradient(0, 0, self.width(), 0)
        steps = 12
        for index in range(steps + 1):
            fraction = index / steps
            kelvin = self.minimum + (self.maximum - self.minimum) * fraction
            gradient.setColorAt(fraction, QColor(*xy_to_rgb(kelvin_to_xy(kelvin), 1.0)))

        painter.setPen(QPen(QColor(0, 0, 0, 50), 1))
        painter.setBrush(gradient)
        painter.drawRoundedRect(QRectF(0.5, 0.5, self.width() - 1, self.height() - 1), 6, 6)
        painter.end()


class ColorPreview(QWidget):
    """A large swatch of what the bulb is about to look like."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._color = QColor(255, 255, 255)
        self._caption = ""
        self.setMinimumHeight(70)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_color(self, color: QColor, caption: str = "") -> None:
        self._color = QColor(color)
        self._caption = caption
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        rect = QRectF(1, 1, self.width() - 2, self.height() - 2)
        painter.setPen(QPen(QColor(0, 0, 0, 45), 1))
        painter.setBrush(self._color)
        painter.drawRoundedRect(rect, 10, 10)

        if self._caption:
            # Dark text on pale colours, light text on deep ones, so the caption
            # stays legible across the whole range.
            luminance = (
                0.2126 * self._color.redF()
                + 0.7152 * self._color.greenF()
                + 0.0722 * self._color.blueF()
            )
            painter.setPen(QColor(20, 20, 20) if luminance > 0.5 else QColor(245, 245, 245))
            painter.drawText(rect, Qt.AlignCenter, self._caption)
        painter.end()
