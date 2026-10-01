"""Главное окно Джарвиса.

Фоновые потоки не трогают виджеты: они кладут события в очередь (MainWindow.post),
а окно разбирает её в главном потоке через after().
"""

from __future__ import annotations

import logging
import queue
import tkinter as tk

import customtkinter as ctk

from . import icons, theme
from .dialogs import ConfirmDialog, SettingsDialog
from .widgets import ArcReactor, ChatView, LogView, VoiceBars

log = logging.getLogger(__name__)

BACKENDS = {"ollama": "Ollama", "gigachat": "GigaChat", "gemini": "Gemini", "qwen": "Qwen"}
_BACKEND_BY_TITLE = {title: name for name, title in BACKENDS.items()}
# Подсказки-примеры: нажатие отправляет команду (заодно видно, что Джарвис управляет ПК).
QUICK_COMMANDS = ["Какая погода?", "Открой браузер", "Сделай погромче", "Который час?"]
WINDOW_SIZE = (1380, 860)


class MainWindow(ctk.CTk):
    def __init__(self, app):
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        super().__init__(fg_color=theme.BG)
        self.app = app
        self.events: queue.Queue = queue.Queue()
        self._closing = False
        self._chat_request = 0
        self._bubble = None
        self._log_card = None
        self._state = "loading"
        self._listening = False
        self._pulse_on = False
        self._settings: SettingsDialog | None = None

        self.title("Джарвис")
        self.fonts = theme.Fonts()
        self._scale = self._get_window_scaling()
        self._fit_to_screen()
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Escape>", lambda _event: self.app.stop())
        self.bind_all("<Control-KeyPress>", self._on_ctrl_key, add="+")
        self.after(40, self._poll)
        self.after(250, self._track_focus)
        self.after(450, self._pulse_mic)

    # ─── построение окна ───
    def _build(self) -> None:
        # Ширина: слева — индикатор (постоянная), чат получает основную часть свободного места.
        self.grid_columnconfigure(0, weight=0, minsize=self._px(290))
        self.grid_columnconfigure(1, weight=3)
        self.grid_columnconfigure(2, weight=1, minsize=self._px(300))
        self.grid_rowconfigure(1, weight=1)
        self._build_header()
        self._build_left()
        self._build_chat()
        self._build_log()
        self._build_input()
        self.entry.focus_set()

    def _px(self, value: float) -> int:
        """Размер для «сырых» tk.Canvas с учётом масштаба экрана."""
        return int(value * self._scale)

    def _fit_to_screen(self) -> None:
        """Окно не больше экрана: на ноутбуке с масштабом 125–150 % 1380×860 может не поместиться."""
        width, height = WINDOW_SIZE
        screen_w = self.winfo_screenwidth() / self._scale
        screen_h = self.winfo_screenheight() / self._scale
        fit_w, fit_h = min(width, int(screen_w - 40)), min(height, int(screen_h - 110))  # минус панель задач
        self.minsize(min(1040, fit_w), min(600, fit_h))
        x = max(0, int((screen_w - fit_w) / 2 * self._scale))
        y = max(0, int((screen_h - 40 - fit_h) / 3 * self._scale))
        self.geometry(f"{fit_w}x{fit_h}+{x}+{y}")
        if fit_w < width or fit_h < height:
            try:
                self.after(50, lambda: self.state("zoomed"))  # маленький экран — сразу на весь экран
            except tk.TclError:
                pass

    def _build_header(self) -> None:
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, columnspan=3, sticky="ew", padx=20, pady=(14, 6))
        header.grid_columnconfigure(1, weight=1)
        brand = ctk.CTkFrame(header, fg_color="transparent")
        brand.grid(row=0, column=0, sticky="w")
        self.logo = ArcReactor(brand, size=self._px(44), bg=theme.BG, level=lambda: 0.0)
        self.logo.pack(side="left")
        titles = ctk.CTkFrame(brand, fg_color="transparent")
        titles.pack(side="left", padx=12)
        ctk.CTkLabel(titles, text="J.A.R.V.I.S.", font=self.fonts.title, text_color=theme.TEXT, height=26).pack(anchor="w")
        ctk.CTkLabel(titles, text="голосовой ассистент · управление компьютером", font=self.fonts.small,
                     text_color=theme.MUTED, height=16).pack(anchor="w")

        right = ctk.CTkFrame(header, fg_color="transparent")
        right.grid(row=0, column=2, sticky="e")
        ctk.CTkLabel(right, text="Модель", font=self.fonts.small, text_color=theme.MUTED).pack(side="left", padx=(0, 8))
        self.backend_selector = ctk.CTkSegmentedButton(
            right, values=list(BACKENDS.values()), font=self.fonts.body_bold, height=36,
            fg_color=theme.PANEL_2, selected_color=theme.BLUE, selected_hover_color=theme.BLUE_HOVER,
            unselected_color=theme.PANEL_2, unselected_hover_color=theme.SECONDARY_HOVER, text_color=theme.TEXT,
            command=self._on_backend)
        self.backend_selector.pack(side="left")
        self.model_label = ctk.CTkLabel(right, text="", font=self.fonts.small_bold, text_color=theme.ACCENT,
                                        fg_color=theme.PANEL_2, corner_radius=10, height=32, padx=12)
        self.model_label.pack(side="left", padx=10)
        ctk.CTkButton(right, text="Настройки", width=130, height=38, corner_radius=12, font=self.fonts.body_bold,
                      fg_color=theme.PANEL_2, hover_color=theme.SECONDARY_HOVER, text_color=theme.TEXT,
                      image=icons.icon("gear", theme.TEXT, 18), command=self.open_settings).pack(side="left")

    def _build_left(self) -> None:
        panel = ctk.CTkFrame(self, fg_color=theme.PANEL, corner_radius=18, border_width=1, border_color=theme.BORDER)
        panel.grid(row=1, column=0, sticky="nsew", padx=(20, 8), pady=6)
        self.left_panel = panel
        # Сначала — нижние блоки: при нехватке высоты уменьшается «ядро», а не переключатели.
        hotkey = str(self.app.config.get("hotkey", "ctrl+alt+j")).upper()
        self.tip_label = ctk.CTkLabel(panel, text=f"{hotkey} — сказать команду\nEsc — остановить", font=self.fonts.small,
                                      text_color=theme.FAINT, justify="center")
        self.tip_label.pack(side="bottom", pady=(4, 12))

        self.quick = ctk.CTkFrame(panel, fg_color="transparent")
        ctk.CTkLabel(self.quick, text="ПОПРОБУЙТЕ", font=self.fonts.tiny_bold, text_color=theme.FAINT, anchor="w",
                     height=16).grid(row=0, column=0, columnspan=2, sticky="w", padx=2)
        for index, command in enumerate(QUICK_COMMANDS):
            ctk.CTkButton(self.quick, text=command, height=30, corner_radius=10, font=self.fonts.small,
                          fg_color=theme.PANEL_2, hover_color=theme.SECONDARY_HOVER, text_color=theme.TEXT,
                          border_width=1, border_color=theme.BORDER,
                          command=lambda text=command: self.app.submit_text(text, "text")).grid(
                row=1 + index // 2, column=index % 2, sticky="ew", padx=2, pady=2)
        self.quick.grid_columnconfigure((0, 1), weight=1, uniform="quick")
        self.quick.pack(side="bottom", fill="x", padx=16, pady=(2, 4))

        switches = ctk.CTkFrame(panel, fg_color=theme.PANEL_2, corner_radius=14)
        switches.pack(side="bottom", fill="x", padx=16, pady=(4, 6))
        self.jarvis_switch = ctk.CTkSwitch(switches, text="Режим «Джарвис»", font=self.fonts.body_bold,
                                           text_color=theme.TEXT, progress_color=theme.ACCENT_HOVER,
                                           command=self._on_jarvis_mode)
        self.jarvis_switch.pack(anchor="w", padx=14, pady=(12, 0))
        ctk.CTkLabel(switches, text="всегда слушать и откликаться на «Джарвис, …»", font=self.fonts.small,
                     text_color=theme.MUTED, anchor="w", justify="left", wraplength=230).pack(fill="x", padx=14)
        self.speak_switch = ctk.CTkSwitch(switches, text="Озвучивать ответы", font=self.fonts.body_bold,
                                          text_color=theme.TEXT, progress_color=theme.ACCENT_HOVER,
                                          command=self._on_speak)
        self.speak_switch.pack(anchor="w", padx=14, pady=(8, 12))

        self.reactor = ArcReactor(panel, size=self._px(220), bg=theme.PANEL, level=self._level)
        self.reactor.pack(pady=(14, 0))
        self.state_label = ctk.CTkLabel(panel, text=theme.STATE_TITLES["loading"], font=self.fonts.state,
                                        text_color=theme.STATE_COLORS["loading"])
        self.state_label.pack()
        self.state_hint = ctk.CTkLabel(panel, text="", font=self.fonts.small, text_color=theme.MUTED, wraplength=260,
                                       justify="center")
        self.state_hint.pack(padx=16)
        self.bars = VoiceBars(panel, width=self._px(260), height=self._px(40), bg=theme.PANEL, level=self._mic_level)
        self.bars.pack(pady=(8, 2))
        mic_row = ctk.CTkFrame(panel, fg_color="transparent")
        mic_row.pack(fill="x", padx=22)
        ctk.CTkLabel(mic_row, text="", image=icons.icon("mic", theme.MUTED, 14), width=16).pack(side="left")
        self.mic_label = ctk.CTkLabel(mic_row, text="микрофон не открыт", font=self.fonts.small, text_color=theme.MUTED,
                                      anchor="w")
        self.mic_label.pack(side="left", padx=6, fill="x", expand=True)
        self.heard_label = ctk.CTkLabel(panel, text="", font=self.fonts.body, text_color=theme.ACCENT, wraplength=260,
                                        justify="center", height=40)
        self.heard_label.pack(fill="x", padx=16, pady=(4, 2))
        panel.bind("<Configure>", lambda _event: self.after_idle(self._fit_left), add="+")

    def _fit_left(self) -> None:
        """«Ядро» занимает свободную высоту левой панели (от 110 до 220 точек); если места совсем
        мало — прячем подсказки-примеры."""
        panel = self.left_panel
        height = panel.winfo_height()
        if height <= 1:
            return

        def used(widget) -> int:
            info = widget.pack_info()
            pady = info.get("pady", 0)
            pads = sum(int(p) for p in pady) if isinstance(pady, (tuple, list)) else 2 * int(pady)
            return widget.winfo_reqheight() + pads

        others = [w for w in panel.pack_slaves() if w is not self.reactor]
        free = height - sum(used(w) for w in others) - self._px(20)
        quick_shown = self.quick in others
        if quick_shown and free < self._px(150):
            free += used(self.quick)
            self.quick.pack_forget()
        elif not quick_shown and free - self.quick.winfo_reqheight() - self._px(6) >= self._px(190):
            self.quick.pack(side="bottom", fill="x", padx=16, pady=(2, 4), after=self.tip_label)
            free -= used(self.quick)
        self.reactor.resize(max(self._px(110), min(self._px(220), free)))

    def _build_chat(self) -> None:
        panel = ctk.CTkFrame(self, fg_color=theme.PANEL, corner_radius=18, border_width=1, border_color=theme.BORDER)
        panel.grid(row=1, column=1, sticky="nsew", padx=8, pady=6)
        top = ctk.CTkFrame(panel, fg_color="transparent")
        top.pack(fill="x", padx=18, pady=(14, 6))
        ctk.CTkLabel(top, text="Диалог", font=self.fonts.h2, text_color=theme.TEXT).pack(side="left")
        ctk.CTkButton(top, text="Новый диалог", width=140, height=32, corner_radius=10, font=self.fonts.small_bold,
                      fg_color=theme.PANEL_2, hover_color=theme.SECONDARY_HOVER, text_color=theme.TEXT,
                      image=icons.icon("plus", theme.TEXT, 14), command=self.app.new_dialog).pack(side="right")
        self.chat = ChatView(panel, self.fonts)
        self.chat.pack(fill="both", expand=True, padx=8, pady=(0, 10))

    def _build_log(self) -> None:
        panel = ctk.CTkFrame(self, fg_color=theme.PANEL, corner_radius=18, border_width=1, border_color=theme.BORDER)
        panel.grid(row=1, column=2, sticky="nsew", padx=(8, 20), pady=6)
        top = ctk.CTkFrame(panel, fg_color="transparent")
        top.pack(fill="x", padx=18, pady=(14, 6))
        ctk.CTkLabel(top, text="Журнал действий", font=self.fonts.h2, text_color=theme.TEXT).pack(side="left")
        self.log_view = LogView(panel, self.fonts)
        self.log_view.pack(fill="both", expand=True, padx=8, pady=(0, 10))

    def _build_input(self) -> None:
        bar = ctk.CTkFrame(self, fg_color=theme.PANEL, corner_radius=18, border_width=1, border_color=theme.BORDER)
        bar.grid(row=2, column=0, columnspan=3, sticky="ew", padx=20, pady=(6, 18))
        bar.grid_columnconfigure(0, weight=1)
        self.entry = ctk.CTkEntry(bar, placeholder_text="Напишите команду… или скажите «Джарвис, открой браузер»",
                                  height=50, corner_radius=14, font=self.fonts.input, fg_color=theme.INPUT,
                                  border_width=1, border_color=theme.BORDER, text_color=theme.TEXT,
                                  placeholder_text_color=theme.FAINT)
        self.entry.grid(row=0, column=0, sticky="ew", padx=(12, 8), pady=12)
        self.entry.bind("<Return>", self._on_send)
        self.entry.bind("<KP_Enter>", self._on_send)
        ctk.CTkButton(bar, text="Отправить", width=140, height=50, corner_radius=14, font=self.fonts.body_bold,
                      fg_color=theme.ACCENT_HOVER, hover_color="#0e7490", text_color="#ffffff",
                      image=icons.icon("send", "#ffffff", 18), compound="right",
                      command=self._on_send).grid(row=0, column=1, padx=4)
        self.mic_button = ctk.CTkButton(bar, text="Микрофон", width=150, height=50, corner_radius=14,
                                        font=self.fonts.body_bold, fg_color=theme.BLUE, hover_color=theme.BLUE_HOVER,
                                        text_color="#ffffff", image=icons.icon("mic", "#ffffff", 18),
                                        command=self.app.toggle_mic)
        self.mic_button.grid(row=0, column=2, padx=4)
        ctk.CTkButton(bar, text="Стоп", width=120, height=50, corner_radius=14, font=self.fonts.body_bold,
                      fg_color=theme.DANGER, hover_color=theme.DANGER_HOVER, text_color="#ffffff",
                      image=icons.icon("stop", "#ffffff", 16), command=self.app.stop).grid(row=0, column=3, padx=(4, 12))

    # ─── уровни звука для анимации ───
    def _mic_level(self) -> float:
        if self._state == "speaking":
            return getattr(self.app.speaker, "level", 0.0)
        listener = self.app.listener
        return listener.level if listener.stream_open else 0.0

    def _level(self) -> float:
        return self._mic_level()

    # ─── очередь событий ───
    def post(self, kind: str, *args) -> None:
        """Потокобезопасно: вызывается из любых потоков."""
        self.events.put((kind, args))

    def _poll(self) -> None:
        for _ in range(400):
            if self._closing:
                return
            try:
                kind, args = self.events.get_nowait()
            except queue.Empty:
                break
            handler = getattr(self, f"_ev_{kind}", None)
            if handler is None:
                log.warning("Неизвестное событие интерфейса: %s", kind)
                continue
            try:
                handler(*args)
            except Exception:
                log.exception("Ошибка обработки события %s", kind)
        if not self._closing:
            self.after(40, self._poll)

    def _track_focus(self) -> None:
        try:
            self.app.focus.update()
        except Exception:
            pass
        if not self._closing:
            self.after(250, self._track_focus)

    def _pulse_mic(self) -> None:
        if self._closing:
            return
        if self._listening:
            self._pulse_on = not self._pulse_on
            self.mic_button.configure(fg_color="#3b82f6" if self._pulse_on else theme.BLUE_HOVER)
        self.after(450, self._pulse_mic)

    # ─── обработчики событий ───
    def _ev_state(self, state: str, subtitle: str = "") -> None:
        self._state = state
        color = theme.STATE_COLORS.get(state, theme.STATE_COLORS["idle"])
        self.reactor.set_state(state)
        self.logo.set_state(state)
        self.bars.set_color(color)
        self.state_label.configure(text=theme.STATE_TITLES.get(state, state), text_color=color)
        self.state_hint.configure(text=subtitle)

    def _ev_user(self, request_id: int, text: str, source: str) -> None:
        self._bubble = None
        self._chat_request = request_id
        self.chat.add_user(text, voice=source == "voice")

    def _ev_assistant_delta(self, request_id: int, text: str) -> None:
        if request_id != self._chat_request:
            return
        if self._bubble is None:
            text = text.lstrip()
            if not text:
                return
            self._bubble = self.chat.add_jarvis(text)
        else:
            self._bubble.append(text)
            self.chat.scroll_to_end()

    def _ev_assistant_break(self, request_id: int) -> None:
        if request_id == self._chat_request and self._bubble is not None and self._bubble.text and \
                not self._bubble.text[-1].isspace():
            self._bubble.append(" ")

    def _ev_assistant_end(self, request_id: int, suffix: str = "") -> None:
        if request_id == self._chat_request and self._bubble is not None:
            if suffix:
                self._bubble.set_text(self._bubble.text.rstrip() + suffix)
            self._bubble.render_rich()
            self._bubble = None
            self.chat.scroll_to_end()

    def _ev_assistant_message(self, request_id: int, text: str) -> None:
        if request_id == self._chat_request:
            self._bubble = None
            self.chat.add_jarvis(text).render_rich()

    def _ev_system(self, text: str) -> None:
        self.chat.add_system(text)

    def _ev_error(self, message: str, hint: str = "") -> None:
        self._bubble = None
        self.chat.add_error(message, hint)

    def _ev_report(self, title: str, text: str) -> None:
        self.chat.add_report(title, text)

    def _ev_clear(self) -> None:
        self._bubble = None
        self.chat.clear()

    def _ev_log_start(self, time_text: str, tool: str, arguments: str) -> None:
        self._log_card = self.log_view.add(time_text, tool, arguments)

    def _ev_log_result(self, status: str, text: str) -> None:
        if self._log_card is None:
            self._log_card = self.log_view.add("", "—", "")
        self._log_card.finish(status, text)
        self._log_card = None

    def _ev_heard(self, text: str) -> None:
        self.heard_label.configure(text=f"«{text}»" if text else "")

    def _ev_mic(self, listening: bool) -> None:
        self._listening = listening
        if listening:
            self.mic_button.configure(text="Слушаю…")
        else:
            self.mic_button.configure(text="Микрофон", fg_color=theme.BLUE)

    def _ev_mic_device(self, name: str) -> None:
        self.mic_label.configure(text=name or "микрофон не открыт")

    def _ev_voice_available(self, available: bool) -> None:
        state = "normal" if available else "disabled"
        self.mic_button.configure(state=state)
        self.jarvis_switch.configure(state=state)

    @staticmethod
    def _set_switch(switch: ctk.CTkSwitch, value: bool) -> None:
        state = switch.cget("state")
        switch.configure(state="normal")  # у выключенного переключателя select() игнорируется
        (switch.select if value else switch.deselect)()
        switch.configure(state=state)

    def _ev_settings(self, jarvis_mode: bool, speak: bool) -> None:
        self._set_switch(self.jarvis_switch, jarvis_mode)
        self._set_switch(self.speak_switch, speak)

    def _ev_backend(self, name: str, model: str) -> None:
        self.backend_selector.set(BACKENDS.get(name, name))
        self.model_label.configure(text=model)

    def _ev_confirm(self, request) -> None:
        if self.state() in ("iconic", "withdrawn"):
            self.deiconify()
        ConfirmDialog(self, request, self.fonts)

    def _ev_call(self, func, *args) -> None:
        func(*args)

    # ─── действия пользователя ───
    @staticmethod
    def _on_ctrl_key(event):
        """Ctrl+V/C/X/A в полях ввода и при русской раскладке: Tk в Windows смотрит на букву,
        а не на клавишу, поэтому «Ctrl+М» не вставляет текст (ключ API, команду)."""
        if event.keysym.lower() in ("v", "c", "x", "a"):
            return None  # латинская раскладка — стандартная обработка
        action = {86: "<<Paste>>", 67: "<<Copy>>", 88: "<<Cut>>", 65: "<<SelectAll>>"}.get(event.keycode)
        if action is None or not isinstance(event.widget, (tk.Entry, tk.Text)):
            return None
        event.widget.event_generate(action)
        return "break"

    def open_settings(self) -> None:
        if self._settings is not None:
            try:
                if self._settings.winfo_exists():
                    self._settings.lift()
                    self._settings.focus_force()
                    return
            except tk.TclError:
                pass
        self._settings = SettingsDialog(self, self.app, self.fonts)

    def _on_send(self, _event=None) -> str:
        text = self.entry.get().strip()
        if text:
            self.entry.delete(0, "end")
            self.app.submit_text(text, "text")
        return "break"

    def _on_jarvis_mode(self) -> None:
        self.app.set_jarvis_mode(bool(self.jarvis_switch.get()))

    def _on_speak(self) -> None:
        self.app.set_speak(bool(self.speak_switch.get()))

    def _on_backend(self, title: str) -> None:
        self.app.set_backend(_BACKEND_BY_TITLE.get(title, "ollama"))

    def _on_close(self) -> None:
        self._closing = True
        try:
            self.app.shutdown()
        finally:
            self.destroy()
