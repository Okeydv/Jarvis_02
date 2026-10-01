"""Виджеты: анимированное «ядро», полоса уровня звука, чат с пузырями, журнал действий."""

from __future__ import annotations

import math
import tkinter as tk
from collections import deque
from datetime import datetime
from typing import Callable

import customtkinter as ctk

from . import theme


def _hidden(widget: tk.Misc) -> bool:
    try:
        return widget.winfo_toplevel().state() == "iconic" or not widget.winfo_ismapped()
    except tk.TclError:
        return True


class ArcReactor(tk.Canvas):
    """Анимированный индикатор состояния в стиле реактора Старка.

    Цвет плавно меняется по состоянию, кольца вращаются (быстрее, когда Джарвис думает),
    ядро пульсирует в такт голосу пользователя или ответу.
    """

    _SPEED = {"loading": 2.5, "idle": 0.35, "listening": 1.8, "thinking": 5.0, "executing": -6.0, "speaking": 1.2}

    def __init__(self, master, size: int, bg: str, level: Callable[[], float]):
        super().__init__(master, width=size, height=size, bg=bg, highlightthickness=0, bd=0)
        self.size, self.bg, self.level = size, bg, level
        self.state = "loading"
        self._color = theme.STATE_COLORS["loading"]
        self._t = 0.0
        self._pulse = 0.0
        self._angles = [0.0, 120.0, 240.0]
        self._dash = 0
        self._glow = [self.create_oval(0, 0, 0, 0, outline="", fill=bg) for _ in range(8)]
        self._ring = self.create_oval(0, 0, 0, 0, outline=bg, width=1)
        self._ticks = [self.create_line(0, 0, 0, 0, fill=bg, width=1) for _ in range(36)]
        self._arcs = [self.create_arc(0, 0, 0, 0, style="arc", outline=bg) for _ in range(3)]
        self._inner = self.create_oval(0, 0, 0, 0, outline=bg, width=1, dash=(3, 5))
        self._core_glow = self.create_oval(0, 0, 0, 0, outline="", fill=bg)
        self._core = self.create_oval(0, 0, 0, 0, outline="", fill=bg)
        self._core_hi = self.create_oval(0, 0, 0, 0, outline="", fill=bg)
        self._layout()
        self.after(33, self._tick)

    def _layout(self) -> None:
        """Неподвижные части (кольцо, риски, толщина дуг) — под текущий размер."""
        c, r = self.size / 2, self.size * 0.46
        self._c, self._r = c, r
        self.coords(self._ring, c - r * 0.97, c - r * 0.97, c + r * 0.97, c + r * 0.97)
        for i, item in enumerate(self._ticks):
            angle = math.radians(i * 10)
            inner = r * (0.88 if i % 3 == 0 else 0.91)
            self.coords(item, c + inner * math.cos(angle), c + inner * math.sin(angle),
                        c + r * 0.94 * math.cos(angle), c + r * 0.94 * math.sin(angle))
        width = max(2, int(self.size * 0.02))
        for item in self._arcs:
            self.itemconfigure(item, width=width)

    def resize(self, size: int) -> None:
        size = int(size)
        if size == self.size or size < 16:
            return
        self.size = size
        self.configure(width=size, height=size)
        self._layout()

    def set_state(self, state: str) -> None:
        self.state = state

    def _tick(self) -> None:
        try:
            if not self.winfo_exists():
                return
            if _hidden(self):
                self.after(250, self._tick)
                return
            self._draw()
            self.after(33, self._tick)
        except tk.TclError:
            return

    def _draw(self) -> None:
        target = theme.STATE_COLORS.get(self.state, theme.STATE_COLORS["idle"])
        self._color = theme.blend(self._color, target, 0.15)
        color, bg, c, r = self._color, self.bg, self._c, self._r
        self._t += 1 / 30
        level = max(0.0, min(1.0, self.level() or 0.0))
        self._pulse = self._pulse * 0.65 + level * 0.35
        if self.state in ("listening", "speaking"):
            breath = self._pulse * 0.42
        elif self.state == "thinking":
            breath = 0.05 * math.sin(self._t * 6)
        elif self.state == "executing":
            breath = 0.05 * math.sin(self._t * 9)
        else:
            breath = 0.035 * math.sin(self._t * 1.6)
        speed = self._SPEED.get(self.state, 0.5)
        for i, factor in enumerate((1.0, -1.6, 2.3)):
            self._angles[i] = (self._angles[i] + speed * factor) % 360

        for i, item in enumerate(self._glow):
            radius = r * (1.0 - i * 0.07) * (1 + breath * 0.25)
            self.coords(item, c - radius, c - radius, c + radius, c + radius)
            self.itemconfigure(item, fill=theme.blend(bg, color, 0.025 + i * 0.022 + self._pulse * 0.05))
        self.itemconfigure(self._ring, outline=theme.blend(bg, color, 0.5))
        tick_color = theme.blend(bg, color, 0.35)
        for item in self._ticks:
            self.itemconfigure(item, fill=tick_color)
        arc_r = r * 0.80
        for i, (item, extent) in enumerate(zip(self._arcs, (78, 48, 26))):
            self.coords(item, c - arc_r, c - arc_r, c + arc_r, c + arc_r)
            self.itemconfigure(item, start=self._angles[i], extent=extent,
                               outline=theme.blend(bg, color, 0.95 - i * 0.18))
        inner_r = r * 0.64
        self._dash = (self._dash + (2 if speed >= 0 else -2)) % 8
        self.coords(self._inner, c - inner_r, c - inner_r, c + inner_r, c + inner_r)
        self.itemconfigure(self._inner, outline=theme.blend(bg, color, 0.55), dashoffset=self._dash)
        glow_r = r * 0.44 * (1 + breath)
        core_r = r * 0.30 * (1 + breath)
        hi_r = r * 0.13 * (1 + breath * 0.5)
        self.coords(self._core_glow, c - glow_r, c - glow_r, c + glow_r, c + glow_r)
        self.itemconfigure(self._core_glow, fill=theme.blend(bg, color, 0.28 + self._pulse * 0.2))
        self.coords(self._core, c - core_r, c - core_r, c + core_r, c + core_r)
        self.itemconfigure(self._core, fill=color)
        self.coords(self._core_hi, c - hi_r, c - hi_r, c + hi_r, c + hi_r)
        self.itemconfigure(self._core_hi, fill=theme.blend(color, "#ffffff", 0.55))


class VoiceBars(tk.Canvas):
    """Бегущая полоса уровня звука: видно, слышит ли микрофон."""

    def __init__(self, master, width: int, height: int, bg: str, level: Callable[[], float], bars: int = 34):
        super().__init__(master, width=width, height=height, bg=bg, highlightthickness=0, bd=0)
        self.bg, self.level, self.color = bg, level, theme.ACCENT
        self._height = height
        self._history: deque[float] = deque([0.0] * bars, maxlen=bars)
        step = width / bars
        self._x = [step * (i + 0.5) for i in range(bars)]
        bar_width = max(2, int(step * 0.55))
        self._bars = [self.create_line(x, height / 2, x, height / 2, width=bar_width, capstyle="round", fill=bg)
                      for x in self._x]
        self.after(50, self._tick)

    def set_color(self, color: str) -> None:
        self.color = color

    def _tick(self) -> None:
        try:
            if not self.winfo_exists():
                return
            if _hidden(self):
                self.after(250, self._tick)
                return
            self._history.append(max(0.0, min(1.0, self.level() or 0.0)))
            mid = self._height / 2
            for x, item, value in zip(self._x, self._bars, self._history):
                half = max(1.5, value * (self._height * 0.46))
                self.coords(item, x, mid - half, x, mid + half)
                self.itemconfigure(item, fill=theme.blend(self.bg, self.color, 0.25 + value * 0.75))
            self.after(50, self._tick)
        except tk.TclError:
            return


class Bubble(ctk.CTkFrame):
    """Сообщение в чате."""

    STYLES = {
        "user": (theme.USER_BUBBLE, theme.USER_BUBBLE, theme.USER_TEXT, "#bfdbfe"),
        "jarvis": (theme.JARVIS_BUBBLE, theme.BORDER, theme.TEXT, theme.ACCENT),
        "error": (theme.ERROR_BG, theme.ERROR_BORDER, theme.ERROR_TEXT, "#fca5a5"),
        "report": (theme.CARD, theme.BORDER, theme.TEXT, theme.ACCENT),
    }

    def __init__(self, master, role: str, title: str, text: str, fonts: theme.Fonts, wrap: int, hint: str = ""):
        bg, border, fg, title_color = self.STYLES[role]
        super().__init__(master, fg_color=bg, corner_radius=16, border_width=1, border_color=border)
        self.text = text
        self._header = ctk.CTkLabel(self, text=title, font=fonts.tiny_bold, text_color=title_color, anchor="w", height=16)
        self._header.pack(fill="x", padx=14, pady=(8, 0))
        body_font = fonts.mono if role == "report" else fonts.body
        self.body = ctk.CTkLabel(self, text=text, font=body_font, text_color=fg, justify="left", anchor="w",
                                 wraplength=wrap)
        self.body.pack(fill="x", padx=14, pady=(2, 10 if not hint else 2))
        self.hint = None
        if hint:
            self.hint = ctk.CTkLabel(self, text="Как исправить: " + hint, font=fonts.small, text_color=theme.HINT,
                                     justify="left", anchor="w", wraplength=wrap)
            self.hint.pack(fill="x", padx=14, pady=(0, 10))
        for widget in (self, self.body, self._header):
            widget.bind("<Button-3>", self._menu)

    def append(self, delta: str) -> None:
        self.text += delta
        self.body.configure(text=self.text)

    def set_text(self, text: str) -> None:
        self.text = text
        self.body.configure(text=text)

    def set_wrap(self, wrap: int) -> None:
        self.body.configure(wraplength=wrap)
        if self.hint is not None:
            self.hint.configure(wraplength=wrap)

    def _menu(self, event) -> None:
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Копировать текст", command=self._copy)
        menu.tk_popup(event.x_root, event.y_root)

    def _copy(self) -> None:
        self.clipboard_clear()
        self.clipboard_append(self.text)


class ChatView(ctk.CTkScrollableFrame):
    """Лента сообщений с пузырями и автопрокруткой."""

    MAX_ROWS = 300

    def __init__(self, master, fonts: theme.Fonts):
        super().__init__(master, fg_color=theme.PANEL, corner_radius=0, scrollbar_button_color=theme.BORDER,
                         scrollbar_button_hover_color=theme.FAINT)
        self.fonts = fonts
        self._rows: deque = deque()
        self._bubbles: list[Bubble] = []
        self._wrap = 520
        self._scroll_pending = False
        self._parent_canvas.bind("<Configure>", self._on_resize, add="+")

    def _on_resize(self, event) -> None:
        scaling = self._get_widget_scaling()
        wrap = max(220, int(event.width / scaling * 0.68))
        if abs(wrap - self._wrap) > 8:
            at_bottom = self._parent_canvas.yview()[1] >= 0.98
            self._wrap = wrap
            for bubble in self._bubbles:
                bubble.set_wrap(wrap)
            if at_bottom:
                self.scroll_to_end()

    def _row(self) -> ctk.CTkFrame:
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", padx=10, pady=5)
        self._rows.append(row)
        while len(self._rows) > self.MAX_ROWS:
            old = self._rows.popleft()
            self._bubbles = [b for b in self._bubbles if b.master is not old]
            old.destroy()
        return row

    def _bubble(self, role: str, title: str, text: str, side: str, hint: str = "") -> Bubble:
        bubble = Bubble(self._row(), role, title, text, self.fonts, self._wrap, hint)
        bubble.pack(side=side)
        self._bubbles.append(bubble)
        self.scroll_to_end()
        return bubble

    @staticmethod
    def _now() -> str:
        return datetime.now().strftime("%H:%M")

    def add_user(self, text: str, voice: bool) -> Bubble:
        return self._bubble("user", f"{'ВЫ · ГОЛОСОМ' if voice else 'ВЫ'} · {self._now()}", text, "right")

    def add_jarvis(self, text: str = "") -> Bubble:
        return self._bubble("jarvis", f"ДЖАРВИС · {self._now()}", text, "left")

    def add_error(self, message: str, hint: str = "") -> Bubble:
        return self._bubble("error", "ОШИБКА", message, "left", hint)

    def add_report(self, title: str, text: str) -> Bubble:
        return self._bubble("report", title.upper(), text, "left")

    def add_system(self, text: str) -> None:
        row = self._row()
        ctk.CTkLabel(row, text=text, font=self.fonts.small, text_color=theme.MUTED, wraplength=self._wrap + 120,
                     justify="center").pack(pady=2)
        self.scroll_to_end()

    def clear(self) -> None:
        for row in self._rows:
            row.destroy()
        self._rows.clear()
        self._bubbles.clear()

    def scroll_to_end(self) -> None:
        """Автопрокрутка к последнему сообщению (запросы во время потокового ответа объединяются)."""
        if not self._scroll_pending:
            self._scroll_pending = True
            self.after(40, self._scroll_now)

    def _scroll_now(self) -> None:
        self._scroll_pending = False
        try:
            self.update_idletasks()  # сначала пересчитать высоту выросшего пузыря
            self._parent_canvas.yview_moveto(1.0)
        except tk.TclError:
            pass


class LogCard(ctk.CTkFrame):
    def __init__(self, master, time_text: str, tool: str, arguments: str, fonts: theme.Fonts, wrap: int):
        super().__init__(master, fg_color=theme.CARD, corner_radius=12, border_width=1, border_color=theme.BORDER)
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=12, pady=(10, 2))
        self.pill = ctk.CTkLabel(top, text=theme.STATUS_TITLES["pending"], font=fonts.tiny_bold, height=20,
                                 fg_color=theme.STATUS_COLORS["pending"], corner_radius=8, text_color="#0b1220", padx=8)
        self.pill.pack(side="right")
        ctk.CTkLabel(top, text=tool, font=fonts.mono_bold, text_color=theme.ACCENT, anchor="w").pack(
            side="left", fill="x", expand=True)
        self.labels = []
        meta = time_text
        if arguments and arguments != "{}":
            meta = f"{time_text}  {arguments}" if time_text else arguments
        if meta:
            args = ctk.CTkLabel(self, text=meta, font=fonts.mono, text_color=theme.MUTED, wraplength=wrap,
                                justify="left", anchor="w", height=18)
            args.pack(fill="x", padx=12)
            self.labels.append(args)
        self.result = ctk.CTkLabel(self, text="", font=fonts.small, text_color=theme.TEXT, wraplength=wrap,
                                   justify="left", anchor="w", height=18)
        self.result.pack(fill="x", padx=12, pady=(4, 10))
        self.labels.append(self.result)

    def finish(self, status: str, text: str) -> None:
        color = theme.STATUS_COLORS.get(status, theme.STATUS_COLORS["pending"])
        self.pill.configure(text=theme.STATUS_TITLES.get(status, status), fg_color=color)
        self.result.configure(text=text, text_color=theme.TEXT if status == "ok" else theme.blend(color, "#ffffff", 0.45))


class LogView(ctk.CTkScrollableFrame):
    MAX_CARDS = 150

    def __init__(self, master, fonts: theme.Fonts):
        super().__init__(master, fg_color=theme.PANEL, corner_radius=0, scrollbar_button_color=theme.BORDER,
                         scrollbar_button_hover_color=theme.FAINT)
        self.fonts = fonts
        self._cards: deque[LogCard] = deque()
        self._wrap = 280
        self._empty = ctk.CTkLabel(self, text="Здесь появятся действия Джарвиса:\nкакой инструмент вызван,\nс какими аргументами и что получилось.",
                                   font=fonts.small, text_color=theme.FAINT, justify="center")
        self._empty.pack(pady=40)
        self._parent_canvas.bind("<Configure>", self._on_resize, add="+")

    def _on_resize(self, event) -> None:
        wrap = max(180, int(event.width / self._get_widget_scaling()) - 48)
        if abs(wrap - self._wrap) > 8:
            self._wrap = wrap
            for card in self._cards:
                for label in card.labels:
                    label.configure(wraplength=wrap)

    def add(self, time_text: str, tool: str, arguments: str) -> LogCard:
        if self._empty is not None:
            self._empty.destroy()
            self._empty = None
        card = LogCard(self, time_text, tool, arguments, self.fonts, self._wrap)
        card.pack(fill="x", padx=10, pady=5)
        self._cards.append(card)
        while len(self._cards) > self.MAX_CARDS:
            self._cards.popleft().destroy()
        self.after(20, lambda: self._parent_canvas.yview_moveto(1.0))
        return card
