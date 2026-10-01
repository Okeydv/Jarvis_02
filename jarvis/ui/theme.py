"""Цвета и шрифты интерфейса в стиле J.A.R.V.I.S."""

from __future__ import annotations

import sys

import customtkinter as ctk

BG = "#070b12"
PANEL = "#0c131e"
PANEL_2 = "#111a28"
CARD = "#121c2b"
CODE_BG = "#080d16"
NOTE_BG = "#13182b"
NOTE_BORDER = "#3b3f8f"
NOTE_TITLE = "#a5b4fc"
RISK_COLORS = {"read": "#22d3ee", "act": "#60a5fa", "danger": "#f97316"}
BORDER = "#1d2b3f"
INPUT = "#0f1826"
ACCENT = "#22d3ee"
ACCENT_HOVER = "#0891b2"
TEXT = "#e6edf6"
MUTED = "#8aa0b8"
FAINT = "#5c6d83"
USER_BUBBLE = "#1d4ed8"
USER_TEXT = "#f8fafc"
JARVIS_BUBBLE = "#131e2e"
ERROR_BG = "#2a0f16"
ERROR_BORDER = "#7f1d1d"
ERROR_TEXT = "#fecaca"
HINT = "#fbbf24"
DANGER = "#dc2626"
DANGER_HOVER = "#b91c1c"
BLUE = "#2563eb"
BLUE_HOVER = "#1d4ed8"
SECONDARY = "#1f2a3a"
SECONDARY_HOVER = "#2b3a50"

STATE_COLORS = {
    "loading": "#f59e0b",
    "idle": "#94a3b8",
    "listening": "#3b82f6",
    "thinking": "#facc15",
    "executing": "#a855f7",
    "speaking": "#22c55e",
}
STATE_TITLES = {
    "loading": "Загрузка…",
    "idle": "Жду",
    "listening": "Слушаю",
    "thinking": "Думаю",
    "executing": "Выполняю",
    "speaking": "Говорю",
}
STATUS_COLORS = {"ok": "#22c55e", "error": "#ef4444", "cancelled": "#f59e0b", "pending": "#a855f7"}
STATUS_TITLES = {"ok": "готово", "error": "ошибка", "cancelled": "отменено", "pending": "выполняю…"}

FONT_FAMILY = "Segoe UI" if sys.platform == "win32" else "DejaVu Sans"
MONO_FAMILY = "Consolas" if sys.platform == "win32" else "DejaVu Sans Mono"


def hex_to_rgb(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)


def rgb_to_hex(rgb) -> str:
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(round(c)))) for c in rgb)


def blend(a: str, b: str, t: float) -> str:
    """Смешивает два цвета: t=0 — a, t=1 — b."""
    t = max(0.0, min(1.0, t))
    ra, rb = hex_to_rgb(a), hex_to_rgb(b)
    return rgb_to_hex(tuple(x + (y - x) * t for x, y in zip(ra, rb)))


class Fonts:
    """Набор шрифтов (создаётся после окна: CTkFont требует запущенный Tk)."""

    def __init__(self) -> None:
        family, mono = FONT_FAMILY, MONO_FAMILY
        self.title = ctk.CTkFont(family=family, size=21, weight="bold")
        self.h2 = ctk.CTkFont(family=family, size=15, weight="bold")
        self.state = ctk.CTkFont(family=family, size=24, weight="bold")
        self.body = ctk.CTkFont(family=family, size=14)
        self.body_bold = ctk.CTkFont(family=family, size=14, weight="bold")
        self.small = ctk.CTkFont(family=family, size=12)
        self.small_bold = ctk.CTkFont(family=family, size=12, weight="bold")
        self.tiny_bold = ctk.CTkFont(family=family, size=11, weight="bold")
        self.input = ctk.CTkFont(family=family, size=15)
        self.mono = ctk.CTkFont(family=mono, size=12)
        self.mono_bold = ctk.CTkFont(family=mono, size=13, weight="bold")
