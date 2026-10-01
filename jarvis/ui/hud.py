"""HUD — мини-индикатор поверх всех окон: что Джарвис делает прямо сейчас (слушает, думает,
пишет код, нажимает…), даже когда его окно свёрнуто. Щелчок — открыть окно Джарвиса."""

from __future__ import annotations

import time
import tkinter as tk
from typing import Callable

import customtkinter as ctk

from . import theme

SHOW_AFTER_EVENT = 4.0  # сколько секунд держать HUD после события, когда Джарвис уже ждёт


class Hud(ctk.CTkToplevel):
    def __init__(self, master, fonts: theme.Fonts, on_click: Callable[[], None], is_main_visible: Callable[[], bool]):
        super().__init__(master, fg_color=theme.BG)
        self.withdraw()
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        try:
            self.attributes("-alpha", 0.94)
        except tk.TclError:
            pass
        self.on_click = on_click
        self.is_main_visible = is_main_visible
        self._state = "idle"
        self._last_event = 0.0
        self._shown = False
        frame = ctk.CTkFrame(self, fg_color=theme.PANEL, corner_radius=14, border_width=1, border_color=theme.BORDER)
        frame.pack(fill="both", expand=True)
        top = ctk.CTkFrame(frame, fg_color="transparent")
        top.pack(fill="x", padx=12, pady=(8, 0))
        self.dot = tk.Canvas(top, width=14, height=14, bg=theme.PANEL, highlightthickness=0, bd=0)
        self._dot = self.dot.create_oval(2, 2, 12, 12, fill=theme.STATE_COLORS["idle"], outline="")
        self.dot.pack(side="left")
        self.title_label = ctk.CTkLabel(top, text="Джарвис", font=fonts.body_bold, text_color=theme.TEXT, height=20)
        self.title_label.pack(side="left", padx=8)
        self.detail = ctk.CTkLabel(frame, text="", font=fonts.small, text_color=theme.MUTED, anchor="w", justify="left",
                                   wraplength=300, height=18)
        self.detail.pack(fill="x", padx=12, pady=(0, 8))
        for widget in (self, frame, top, self.dot, self.title_label, self.detail):
            widget.bind("<Button-1>", lambda _event: self.on_click())
        self.after(300, self._tick)

    def set_state(self, state: str, subtitle: str = "") -> None:
        if state != self._state:
            self._last_event = time.monotonic()
        self._state = state
        color = theme.STATE_COLORS.get(state, theme.STATE_COLORS["idle"])
        self.dot.itemconfigure(self._dot, fill=color)
        self.title_label.configure(text=f"Джарвис · {theme.STATE_TITLES.get(state, state).lower()}")
        if subtitle and state in ("listening", "loading"):
            self.detail.configure(text=subtitle)

    def set_detail(self, text: str) -> None:
        text = " ".join(text.split())
        self.detail.configure(text=text if len(text) <= 90 else text[:89] + "…")
        self._last_event = time.monotonic()

    def flash(self, text: str) -> None:
        self.set_detail(text)

    def _wanted(self) -> bool:
        if self.is_main_visible():
            return False
        return self._state not in ("idle", "loading") or time.monotonic() - self._last_event < SHOW_AFTER_EVENT

    def _tick(self) -> None:
        try:
            if not self.winfo_exists():
                return
            wanted = self._wanted()
            if wanted and not self._shown:
                self.update_idletasks()
                width = max(self.winfo_reqwidth(), 260)
                x = self.winfo_screenwidth() - width - 24
                self.geometry(f"+{max(0, x)}+24")
                self.deiconify()
                self.lift()
                self._shown = True
            elif not wanted and self._shown:
                self.withdraw()
                self._shown = False
            self.after(300, self._tick)
        except tk.TclError:
            return
