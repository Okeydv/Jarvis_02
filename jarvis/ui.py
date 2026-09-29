"""Окно Джарвиса на CustomTkinter (тёмная тема).

Фоновые потоки не трогают виджеты: они кладут события в очередь (MainWindow.post),
а окно разбирает её в главном потоке через after().
"""

from __future__ import annotations

import logging
import math
import queue
import re
import tkinter as tk

import customtkinter as ctk

log = logging.getLogger(__name__)

STATES = {
    "loading": ("Загрузка…", "#f59e0b"),
    "idle": ("Жду", "#9ca3af"),
    "listening": ("Слушаю", "#3b82f6"),
    "thinking": ("Думаю", "#eab308"),
    "executing": ("Выполняю", "#a855f7"),
    "speaking": ("Говорю", "#22c55e"),
}
BACKENDS = {"ollama": "Ollama", "gigachat": "GigaChat", "gemini": "Gemini"}
_BACKEND_BY_TITLE = {title: name for name, title in BACKENDS.items()}
_NON_BMP_RE = re.compile("[\U00010000-\U0010FFFF]")


class MainWindow(ctk.CTk):
    def __init__(self, app):
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        super().__init__()
        self.app = app
        self.events: queue.Queue = queue.Queue()
        self._closing = False
        self._chat_request = 0
        self._assistant_open = False
        self._log_pending = False

        self.title("Джарвис")
        self.geometry("1200x760")
        self.minsize(940, 560)
        self.font = ctk.CTkFont(family="Segoe UI", size=14)
        self.bold = ctk.CTkFont(family="Segoe UI", size=14, weight="bold")
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Escape>", lambda _event: self.app.stop())
        self.after(40, self._poll)
        self.after(250, self._track_focus)

    # ─── построение окна ───
    def _build(self) -> None:
        self.grid_columnconfigure(0, weight=3)
        self.grid_columnconfigure(1, weight=2)
        self.grid_rowconfigure(1, weight=1)

        top = ctk.CTkFrame(self, corner_radius=12)
        top.grid(row=0, column=0, columnspan=2, sticky="ew", padx=12, pady=(12, 6))
        top.grid_columnconfigure(2, weight=1)
        self.state_dot = ctk.CTkLabel(top, text="●", font=ctk.CTkFont(size=28), text_color=STATES["loading"][1], width=28)
        self.state_dot.grid(row=0, column=0, padx=(14, 6), pady=6)
        self.state_label = ctk.CTkLabel(top, text=STATES["loading"][0], font=ctk.CTkFont(family="Segoe UI", size=17, weight="bold"))
        self.state_label.grid(row=0, column=1, padx=(0, 12), sticky="w")
        self.heard_label = ctk.CTkLabel(top, text="", text_color="#9ca3af", anchor="w", font=self.font)
        self.heard_label.grid(row=0, column=2, sticky="ew", padx=(0, 8))
        self.jarvis_switch = ctk.CTkSwitch(top, text="Режим «Джарвис»", font=self.font, command=self._on_jarvis_mode)
        self.jarvis_switch.grid(row=0, column=3, padx=8)
        self.speak_switch = ctk.CTkSwitch(top, text="Озвучивать ответы", font=self.font, command=self._on_speak)
        self.speak_switch.grid(row=0, column=4, padx=8)
        self.backend_selector = ctk.CTkSegmentedButton(top, values=list(BACKENDS.values()), font=self.font,
                                                       command=self._on_backend)
        self.backend_selector.grid(row=0, column=5, padx=(8, 4))
        self.model_label = ctk.CTkLabel(top, text="", text_color="#9ca3af", font=self.font)
        self.model_label.grid(row=0, column=6, padx=(4, 14))

        chat_frame = ctk.CTkFrame(self, corner_radius=12)
        chat_frame.grid(row=1, column=0, sticky="nsew", padx=(12, 6), pady=6)
        chat_frame.grid_rowconfigure(1, weight=1)
        chat_frame.grid_columnconfigure(0, weight=1)
        header = ctk.CTkFrame(chat_frame, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 0))
        ctk.CTkLabel(header, text="Чат", font=self.bold).pack(side="left")
        ctk.CTkButton(header, text="Новый диалог", width=120, height=26, font=self.font, fg_color="transparent",
                      border_width=1, command=self.app.new_dialog).pack(side="right")
        self.chat = ctk.CTkTextbox(chat_frame, wrap="word", font=self.font)
        self.chat.grid(row=1, column=0, sticky="nsew", padx=10, pady=10)
        for tag, color in (("user_name", "#60a5fa"), ("user", "#e5e7eb"), ("jarvis_name", "#34d399"),
                           ("jarvis", "#f3f4f6"), ("system", "#9ca3af"), ("error", "#f87171"), ("hint", "#fbbf24")):
            self.chat.tag_config(tag, foreground=color)
        self.chat.configure(state="disabled")

        log_frame = ctk.CTkFrame(self, corner_radius=12)
        log_frame.grid(row=1, column=1, sticky="nsew", padx=(6, 12), pady=6)
        log_frame.grid_rowconfigure(1, weight=1)
        log_frame.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(log_frame, text="Журнал действий", font=self.bold).grid(row=0, column=0, sticky="w", padx=12, pady=(10, 0))
        self.log_box = ctk.CTkTextbox(log_frame, wrap="word", font=ctk.CTkFont(family="Consolas", size=13))
        self.log_box.grid(row=1, column=0, sticky="nsew", padx=10, pady=10)
        for tag, color in (("time", "#9ca3af"), ("tool", "#c084fc"), ("args", "#93c5fd"), ("ok", "#86efac"),
                           ("error", "#fca5a5"), ("cancelled", "#fcd34d"), ("pending", "#9ca3af")):
            self.log_box.tag_config(tag, foreground=color)
        self.log_box.configure(state="disabled")

        bottom = ctk.CTkFrame(self, corner_radius=12)
        bottom.grid(row=2, column=0, columnspan=2, sticky="ew", padx=12, pady=(6, 12))
        bottom.grid_columnconfigure(0, weight=1)
        self.entry = ctk.CTkEntry(bottom, placeholder_text="Напишите команду… (Enter — отправить)", height=40, font=self.font)
        self.entry.grid(row=0, column=0, sticky="ew", padx=(10, 6), pady=10)
        self.entry.bind("<Return>", self._on_send)
        self.entry.bind("<KP_Enter>", self._on_send)
        ctk.CTkButton(bottom, text="Отправить", width=120, height=40, font=self.font,
                      command=self._on_send).grid(row=0, column=1, padx=6)
        self.mic_button = ctk.CTkButton(bottom, text="Микрофон", width=130, height=40, font=self.font,
                                        fg_color="#1d4ed8", hover_color="#1e40af", command=self.app.toggle_mic)
        self.mic_button.grid(row=0, column=2, padx=6)
        ctk.CTkButton(bottom, text="Стоп", width=100, height=40, font=self.font, fg_color="#b91c1c",
                      hover_color="#991b1b", command=self.app.stop).grid(row=0, column=3, padx=(6, 10))
        self.entry.focus_set()

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

    # ─── вывод текста ───
    @staticmethod
    def _insert(box: ctk.CTkTextbox, text: str, tag: str | None = None) -> None:
        box.configure(state="normal")
        try:
            box.insert("end", text, tag)
        except tk.TclError:  # старые Tk не умеют символы вне BMP (часть эмодзи)
            box.insert("end", _NON_BMP_RE.sub("", text), tag)
        box.configure(state="disabled")
        box.see("end")

    def _chat_line(self, text: str, tag: str) -> None:
        self._close_assistant()
        self._insert(self.chat, text + "\n\n", tag)

    def _close_assistant(self, suffix: str = "") -> None:
        if self._assistant_open:
            self._insert(self.chat, suffix + "\n\n", "system" if suffix else "jarvis")
            self._assistant_open = False

    # ─── обработчики событий ───
    def _ev_state(self, state: str, subtitle: str = "") -> None:
        title, color = STATES.get(state, STATES["idle"])
        self.state_dot.configure(text_color=color)
        self.state_label.configure(text=f"{title} · {subtitle}" if subtitle else title)

    def _ev_user(self, request_id: int, text: str, source: str) -> None:
        self._close_assistant()
        self._chat_request = request_id
        self._insert(self.chat, "Вы (голосом): " if source == "voice" else "Вы: ", "user_name")
        self._insert(self.chat, text + "\n\n", "user")

    def _ev_assistant_delta(self, request_id: int, text: str) -> None:
        if request_id != self._chat_request:
            return
        if not self._assistant_open:
            text = text.lstrip()
            if not text:
                return
            self._insert(self.chat, "Джарвис: ", "jarvis_name")
            self._assistant_open = True
        self._insert(self.chat, text, "jarvis")

    def _ev_assistant_break(self, request_id: int) -> None:
        if request_id == self._chat_request and self._assistant_open:
            last = self.chat.get("end-2c", "end-1c")
            if last and not last.isspace():
                self._insert(self.chat, " ", "jarvis")

    def _ev_assistant_end(self, request_id: int, suffix: str = "") -> None:
        if request_id == self._chat_request:
            self._close_assistant(suffix)

    def _ev_assistant_message(self, request_id: int, text: str) -> None:
        if request_id == self._chat_request:
            self._ev_assistant_delta(request_id, text)
            self._close_assistant()

    def _ev_system(self, text: str) -> None:
        self._chat_line(text, "system")

    def _ev_error(self, message: str, hint: str = "") -> None:
        self._close_assistant()
        self._insert(self.chat, "⚠ " + message + ("\n" if hint else "\n\n"), "error")
        if hint:
            self._insert(self.chat, "Как исправить: " + hint + "\n\n", "hint")

    def _ev_clear(self) -> None:
        self._assistant_open = False
        self.chat.configure(state="normal")
        self.chat.delete("1.0", "end")
        self.chat.configure(state="disabled")

    def _ev_log_start(self, time_text: str, tool: str, arguments: str) -> None:
        if self._log_pending:
            self._insert(self.log_box, "\n")
        self._insert(self.log_box, time_text + "  ", "time")
        self._insert(self.log_box, tool + "\n", "tool")
        self._insert(self.log_box, "  аргументы: " + arguments + "\n", "args")
        self._log_pending = True

    def _ev_log_result(self, status: str, text: str) -> None:
        label = {"ok": "результат", "error": "ошибка", "cancelled": "отменено"}.get(status, status)
        self._insert(self.log_box, f"  {label}: {text}\n\n", status if status in ("ok", "error", "cancelled") else "pending")
        self._log_pending = False

    def _ev_heard(self, text: str) -> None:
        self.heard_label.configure(text=f"Слышу: {text}" if text else "")

    def _ev_mic(self, listening: bool) -> None:
        if listening:
            self.mic_button.configure(text="Слушаю…", fg_color="#2563eb")
        else:
            self.mic_button.configure(text="Микрофон", fg_color="#1d4ed8")

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
        ConfirmDialog(self, request)

    def _ev_call(self, func, *args) -> None:
        func(*args)

    # ─── действия пользователя ───
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


class ConfirmDialog(ctk.CTkToplevel):
    """«Да» / «Нет» с точным описанием действия. Нет ответа — отмена по таймауту."""

    def __init__(self, master: MainWindow, request):
        super().__init__(master)
        self.request = request
        confirmation = request.confirmation
        self.title("Джарвис — подтверждение")
        self.resizable(False, False)
        self.attributes("-topmost", True)
        self.protocol("WM_DELETE_WINDOW", lambda: self._answer(False, "user"))
        self.bind("<Escape>", lambda _event: self._answer(False, "user"))
        self.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(self, text="⚠ Требуется подтверждение", text_color="#fbbf24",
                     font=ctk.CTkFont(family="Segoe UI", size=18, weight="bold")).grid(
            row=0, column=0, sticky="w", padx=22, pady=(18, 4))
        ctk.CTkLabel(self, text=confirmation.title, font=master.bold, anchor="w").grid(
            row=1, column=0, sticky="w", padx=22)
        ctk.CTkLabel(self, text=confirmation.text, font=master.font, wraplength=560, justify="left", anchor="w").grid(
            row=2, column=0, sticky="ew", padx=22, pady=(6, 6))
        row = 3
        if confirmation.details:
            details = ctk.CTkTextbox(self, width=560, height=150, wrap="word",
                                     font=ctk.CTkFont(family="Consolas", size=13))
            details.insert("1.0", confirmation.details)
            details.configure(state="disabled")
            details.grid(row=row, column=0, sticky="nsew", padx=22, pady=6)
            row += 1
        self.countdown = ctk.CTkLabel(self, text="", text_color="#9ca3af", font=master.font)
        self.countdown.grid(row=row, column=0, sticky="w", padx=22, pady=(4, 0))
        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.grid(row=row + 1, column=0, sticky="e", padx=22, pady=(10, 18))
        ctk.CTkButton(buttons, text="Да", width=120, height=36, font=master.bold, fg_color="#b91c1c",
                      hover_color="#991b1b", command=lambda: self._answer(True, "user")).pack(side="left", padx=(0, 10))
        ctk.CTkButton(buttons, text="Нет", width=120, height=36, font=master.bold, fg_color="#374151",
                      hover_color="#4b5563", command=lambda: self._answer(False, "user")).pack(side="left")
        self._center_over(master)
        self.after(150, self._bring_to_front)
        self._tick()

    def _center_over(self, master: MainWindow) -> None:
        self.update_idletasks()
        width, height = self.winfo_reqwidth(), self.winfo_reqheight()
        x = master.winfo_rootx() + (master.winfo_width() - width) // 2
        y = master.winfo_rooty() + (master.winfo_height() - height) // 3
        if x < 0 or y < 0 or master.state() == "iconic":  # окно свёрнуто — по центру экрана
            x = (self.winfo_screenwidth() - width) // 2
            y = (self.winfo_screenheight() - height) // 3
        self.geometry(f"+{max(0, x)}+{max(0, y)}")

    def _bring_to_front(self) -> None:
        try:
            self.lift()
            self.focus_force()
        except tk.TclError:
            pass

    def _tick(self) -> None:
        if not self.winfo_exists():
            return
        if self.request.done:
            self.destroy()
            return
        remaining = self.request.remaining()
        if remaining <= 0:
            self._answer(False, "timeout")
            return
        self.countdown.configure(text=f"Нет ответа — отмена через {math.ceil(remaining)} с")
        self.after(200, self._tick)

    def _answer(self, value: bool, reason: str) -> None:
        self.request.resolve(value, reason)
        try:
            self.destroy()
        except tk.TclError:
            pass
