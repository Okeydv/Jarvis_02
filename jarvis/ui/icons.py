"""Иконки, нарисованные кодом (Pillow): не нужны файлы и шрифты значков."""

from __future__ import annotations

import math

import customtkinter as ctk
from PIL import Image, ImageDraw

_SUPERSAMPLE = 8  # рисуем крупно и уменьшаем — получаются гладкие края
_cache: dict[tuple, ctk.CTkImage] = {}


def _rgba(color: str) -> tuple[int, int, int, int]:
    color = color.lstrip("#")
    return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16), 255


class _Pen:
    """Рисование в сетке 24×24 с масштабированием."""

    def __init__(self, size: int, color: str):
        self.k = size * _SUPERSAMPLE / 24
        self.size = size
        self.color = _rgba(color)
        self.image = Image.new("RGBA", (size * _SUPERSAMPLE, size * _SUPERSAMPLE), (0, 0, 0, 0))
        self.draw = ImageDraw.Draw(self.image)

    def p(self, *xy: float) -> list[float]:
        return [v * self.k for v in xy]

    def line(self, points: list[tuple[float, float]], width: float = 2.0) -> None:
        flat = [c for point in points for c in self.p(*point)]
        w = max(1, int(width * self.k))
        self.draw.line(flat, fill=self.color, width=w, joint="curve")
        for x, y in (points[0], points[-1]):  # круглые концы
            r = width / 2
            self.draw.ellipse(self.p(x - r, y - r, x + r, y + r), fill=self.color)

    def arc(self, box: tuple, start: float, end: float, width: float = 2.0) -> None:
        self.draw.arc(self.p(*box), start, end, fill=self.color, width=max(1, int(width * self.k)))

    def rounded(self, box: tuple, radius: float, fill: bool = True, width: float = 2.0) -> None:
        if fill:
            self.draw.rounded_rectangle(self.p(*box), radius=radius * self.k, fill=self.color)
        else:
            self.draw.rounded_rectangle(self.p(*box), radius=radius * self.k, outline=self.color,
                                        width=max(1, int(width * self.k)))

    def polygon(self, points: list[tuple[float, float]]) -> None:
        self.draw.polygon([c for point in points for c in self.p(*point)], fill=self.color)

    def circle(self, cx: float, cy: float, r: float, fill: bool = True, width: float = 2.0, clear: bool = False) -> None:
        box = self.p(cx - r, cy - r, cx + r, cy + r)
        if clear:
            self.draw.ellipse(box, fill=(0, 0, 0, 0))
        elif fill:
            self.draw.ellipse(box, fill=self.color)
        else:
            self.draw.ellipse(box, outline=self.color, width=max(1, int(width * self.k)))

    def result(self) -> Image.Image:
        return self.image.resize((self.size * 2, self.size * 2), Image.LANCZOS)


def _mic(pen: _Pen) -> None:
    pen.rounded((8.5, 2.5, 15.5, 14.5), 3.5)
    pen.arc((5, 6, 19, 18.5), 0, 180, 2.0)
    pen.line([(12, 18.5), (12, 21)], 2.0)
    pen.line([(8.5, 21.2), (15.5, 21.2)], 2.0)


def _send(pen: _Pen) -> None:
    pen.polygon([(3.5, 4), (21.5, 12), (3.5, 20), (6.5, 12)])


def _stop(pen: _Pen) -> None:
    pen.rounded((6, 6, 18, 18), 2.5)


def _gear(pen: _Pen) -> None:
    points = []
    teeth = 8
    for i in range(teeth):
        base = math.pi * 2 * i / teeth
        for offset, radius in ((-0.30, 8.0), (-0.17, 10.6), (0.17, 10.6), (0.30, 8.0)):
            angle = base + offset
            points.append((12 + radius * math.cos(angle), 12 + radius * math.sin(angle)))
    pen.polygon(points)
    pen.circle(12, 12, 8.2)
    pen.circle(12, 12, 3.3, clear=True)


def _plus(pen: _Pen) -> None:
    pen.line([(12, 5), (12, 19)], 2.2)
    pen.line([(5, 12), (19, 12)], 2.2)


def _warning(pen: _Pen) -> None:
    pen.polygon([(12, 2.5), (22.5, 21), (1.5, 21)])
    pen.draw.rectangle(pen.p(11, 8.5, 13, 15.5), fill=(0, 0, 0, 0))
    pen.circle(12, 18, 1.2, clear=True)


def _refresh(pen: _Pen) -> None:
    pen.arc((4, 4, 20, 20), 40, 330, 2.1)
    pen.polygon([(15.5, 2.8), (21.2, 6.9), (14.7, 9.3)])


def _play(pen: _Pen) -> None:
    pen.polygon([(7, 4.5), (19.5, 12), (7, 19.5)])


def _check(pen: _Pen) -> None:
    pen.line([(4.5, 12.5), (9.5, 17.5), (19.5, 6.5)], 2.4)


def _folder(pen: _Pen) -> None:
    pen.rounded((2.5, 5, 21.5, 19.5), 2.5, fill=False, width=2.0)
    pen.line([(2.5, 9), (21.5, 9)], 2.0)


_DRAWERS = {
    "mic": _mic, "send": _send, "stop": _stop, "gear": _gear, "plus": _plus, "warning": _warning,
    "refresh": _refresh, "play": _play, "check": _check, "folder": _folder,
}


def image(name: str, color: str = "#ffffff", size: int = 20) -> Image.Image:
    pen = _Pen(size, color)
    _DRAWERS[name](pen)
    return pen.result()


def icon(name: str, color: str = "#ffffff", size: int = 20) -> ctk.CTkImage:
    key = (name, color, size)
    if key not in _cache:
        img = image(name, color, size)
        _cache[key] = ctk.CTkImage(light_image=img, dark_image=img, size=(size, size))
    return _cache[key]
