"""Диалоги: подтверждение опасного действия и настройки."""

from __future__ import annotations

import math
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk

import customtkinter as ctk

from ..config import ROOT_DIR, mask_secret
from ..stt import PRIVACY_HINT
from . import icons, theme

DEFAULT_MIC = "По умолчанию (системный)"
VOICES = ["aidar", "baya", "kseniya", "xenia", "eugene"]
GIGACHAT_MODELS = ["GigaChat-2", "GigaChat-2-Pro", "GigaChat-2-Max", "GigaChat-3-Ultra"]
GEMINI_MODELS = ["gemini-flash-latest", "gemini-flash-lite-latest"]
QWEN_MODELS = ["qwen/qwen3.7-flash:free", "qwen/qwen3.7-flash", "qwen/qwen3.7-plus", "qwen/qwen3.8-max",
               "qwen/qwen3-max"]


def center_over(window: tk.Toplevel, master: tk.Misc) -> None:
    window.update_idletasks()
    width, height = window.winfo_reqwidth(), window.winfo_reqheight()
    x = master.winfo_rootx() + (master.winfo_width() - width) // 2
    y = master.winfo_rooty() + (master.winfo_height() - height) // 3
    if x < 0 or y < 0 or master.winfo_toplevel().state() == "iconic":
        x = (window.winfo_screenwidth() - width) // 2
        y = (window.winfo_screenheight() - height) // 3
    window.geometry(f"+{max(0, x)}+{max(0, y)}")


def open_in_explorer(path) -> None:
    if sys.platform == "win32":
        os.startfile(str(path))  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", str(path)])


class ConfirmDialog(ctk.CTkToplevel):
    """«Да» / «Нет» с точным описанием действия. Нет ответа — отмена по таймауту."""

    def __init__(self, master, request, fonts: theme.Fonts):
        super().__init__(master, fg_color=theme.PANEL)
        self.request = request
        confirmation = request.confirmation
        self._total = max(0.1, request.remaining())
        self.title("Джарвис — подтверждение")
        self.resizable(False, False)
        self.attributes("-topmost", True)
        self.protocol("WM_DELETE_WINDOW", lambda: self._answer(False, "user"))
        self.bind("<Escape>", lambda _event: self._answer(False, "user"))

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=26, pady=22)
        head = ctk.CTkFrame(body, fg_color="transparent")
        head.pack(fill="x")
        ctk.CTkLabel(head, text="", image=icons.icon("warning", theme.HINT, 30), width=34).pack(side="left")
        titles = ctk.CTkFrame(head, fg_color="transparent")
        titles.pack(side="left", padx=12)
        ctk.CTkLabel(titles, text="НУЖНО ВАШЕ ПОДТВЕРЖДЕНИЕ", font=fonts.tiny_bold, text_color=theme.HINT,
                     anchor="w", height=16).pack(anchor="w")
        ctk.CTkLabel(titles, text=confirmation.title, font=fonts.title, text_color=theme.TEXT, anchor="w").pack(anchor="w")
        ctk.CTkLabel(body, text=confirmation.text, font=fonts.body, text_color=theme.TEXT, wraplength=540,
                     justify="left", anchor="w").pack(fill="x", pady=(16, 6))
        if confirmation.image is not None:  # например, место клика на снимке экрана
            picture = confirmation.image
            scale = min(1.0, 420 / max(picture.width, picture.height))
            self._image = ctk.CTkImage(light_image=picture, dark_image=picture,
                                       size=(int(picture.width * scale), int(picture.height * scale)))
            ctk.CTkLabel(body, text="", image=self._image).pack(pady=6)
        if confirmation.details:
            details = ctk.CTkTextbox(body, width=560, height=150, wrap="word", font=fonts.mono, fg_color=theme.INPUT,
                                     border_width=1, border_color=theme.BORDER, text_color=theme.TEXT)
            details.insert("1.0", confirmation.details)
            details.configure(state="disabled")
            details.pack(fill="x", pady=6)
        self.progress = ctk.CTkProgressBar(body, height=6, progress_color=theme.HINT, fg_color=theme.BORDER)
        self.progress.pack(fill="x", pady=(14, 4))
        self.progress.set(1.0)
        self.countdown = ctk.CTkLabel(body, text="", font=fonts.small, text_color=theme.MUTED, anchor="w")
        self.countdown.pack(fill="x")
        buttons = ctk.CTkFrame(body, fg_color="transparent")
        buttons.pack(fill="x", pady=(18, 0))
        ctk.CTkButton(buttons, text="Нет", width=140, height=42, font=fonts.body_bold, fg_color=theme.SECONDARY,
                      hover_color=theme.SECONDARY_HOVER, command=lambda: self._answer(False, "user")).pack(side="right")
        ctk.CTkButton(buttons, text="Да, выполнить", width=170, height=42, font=fonts.body_bold,
                      fg_color=theme.DANGER, hover_color=theme.DANGER_HOVER,
                      command=lambda: self._answer(True, "user")).pack(side="right", padx=(0, 10))
        center_over(self, master)
        self.after(150, self._bring_to_front)
        self._tick()

    def _bring_to_front(self) -> None:
        try:
            self.lift()
            self.focus_force()
        except tk.TclError:
            pass

    def _tick(self) -> None:
        try:
            if not self.winfo_exists():
                return
            if self.request.done:
                self.destroy()
                return
            remaining = self.request.remaining()
            if remaining <= 0:
                self._answer(False, "timeout")
                return
            self.progress.set(remaining / self._total)
            self.countdown.configure(text=f"Нет ответа — отмена через {math.ceil(remaining)} с")
            self.after(100, self._tick)
        except tk.TclError:
            return

    def _answer(self, value: bool, reason: str) -> None:
        self.request.resolve(value, reason)
        try:
            self.destroy()
        except tk.TclError:
            pass


class SettingsDialog(ctk.CTkToplevel):
    """Настройки: микрофон и голос, доступ к ПК, модели, диагностика."""

    def __init__(self, master, app, fonts: theme.Fonts):
        super().__init__(master, fg_color=theme.BG)
        self.app = app
        self.fonts = fonts
        self.cfg = app.config
        self._events: queue.Queue = queue.Queue()
        self._busy = False
        self._secrets: dict[str, ctk.CTkEntry] = {}
        self.title("Джарвис — настройки")
        self.geometry("680x660")
        self.minsize(620, 560)
        self.transient(master)
        self.bind("<Escape>", lambda _event: self.destroy())

        tabs = ctk.CTkTabview(self, fg_color=theme.PANEL, corner_radius=14, border_width=1, border_color=theme.BORDER,
                              segmented_button_fg_color=theme.PANEL_2, segmented_button_selected_color=theme.BLUE,
                              segmented_button_selected_hover_color=theme.BLUE_HOVER,
                              segmented_button_unselected_color=theme.PANEL_2,
                              segmented_button_unselected_hover_color=theme.SECONDARY_HOVER, text_color=theme.TEXT)
        tabs.pack(fill="both", expand=True, padx=16, pady=(10, 0))
        self._build_voice(tabs.add("Голос"))
        self._build_pc(tabs.add("Доступ к ПК"))
        self._build_models(tabs.add("Модели"))
        self._build_memory(tabs.add("Память"))
        self._build_diagnostics(tabs.add("Диагностика"))
        self.tabs = tabs

        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.pack(fill="x", padx=16, pady=12)
        self.saved = ctk.CTkLabel(bottom, text="", font=fonts.small, text_color=theme.MUTED)
        self.saved.pack(side="left")
        ctk.CTkButton(bottom, text="Закрыть", width=120, height=38, fg_color=theme.SECONDARY,
                      hover_color=theme.SECONDARY_HOVER, font=fonts.body_bold, command=self.destroy).pack(side="right")
        ctk.CTkButton(bottom, text="Сохранить", width=140, height=38, fg_color=theme.BLUE, hover_color=theme.BLUE_HOVER,
                      font=fonts.body_bold, command=self._save).pack(side="right", padx=(0, 10))
        center_over(self, master)
        self.after(150, self._bring_to_front)
        self.after(100, self._poll)

    def _bring_to_front(self) -> None:
        try:
            self.lift()
            self.focus_force()
        except tk.TclError:
            pass

    # ─── вспомогательное ───
    def _section(self, parent, title: str, note: str = "") -> ctk.CTkFrame:
        ctk.CTkLabel(parent, text=title, font=self.fonts.h2, text_color=theme.TEXT, anchor="w").pack(fill="x", pady=(10, 0))
        if note:
            ctk.CTkLabel(parent, text=note, font=self.fonts.small, text_color=theme.MUTED, anchor="w", justify="left",
                         wraplength=580).pack(fill="x")
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=(6, 2))
        return row

    def _option(self, parent, values: list[str], current: str, width: int = 360) -> ctk.CTkOptionMenu:
        menu = ctk.CTkOptionMenu(parent, values=values, width=width, height=34, font=self.fonts.body,
                                 dropdown_font=self.fonts.body, fg_color=theme.INPUT, button_color=theme.SECONDARY,
                                 button_hover_color=theme.SECONDARY_HOVER, text_color=theme.TEXT,
                                 dropdown_fg_color=theme.PANEL_2, dropdown_hover_color=theme.SECONDARY_HOVER,
                                 dropdown_text_color=theme.TEXT, dynamic_resizing=False)
        menu.set(current)
        return menu

    def _entry(self, parent, value: str, width: int = 360) -> ctk.CTkEntry:
        entry = ctk.CTkEntry(parent, width=width, height=34, font=self.fonts.body, fg_color=theme.INPUT,
                             border_color=theme.BORDER, text_color=theme.TEXT)
        entry.insert(0, value)
        return entry

    def _combo(self, parent, values: list[str], current: str, width: int = 360) -> ctk.CTkComboBox:
        combo = ctk.CTkComboBox(parent, values=values, width=width, height=34, font=self.fonts.body,
                                dropdown_font=self.fonts.body, fg_color=theme.INPUT, border_color=theme.BORDER,
                                button_color=theme.SECONDARY, button_hover_color=theme.SECONDARY_HOVER,
                                text_color=theme.TEXT, dropdown_fg_color=theme.PANEL_2,
                                dropdown_hover_color=theme.SECONDARY_HOVER, dropdown_text_color=theme.TEXT)
        combo.set(current)
        return combo

    def _switch(self, parent, text: str, value: bool) -> ctk.CTkSwitch:
        switch = ctk.CTkSwitch(parent, text=text, font=self.fonts.body, text_color=theme.TEXT,
                               progress_color=theme.ACCENT_HOVER, button_color=theme.TEXT, button_hover_color="#ffffff")
        if value:
            switch.select()
        return switch

    def _button(self, parent, text: str, command, icon_name: str | None = None, width: int = 180) -> ctk.CTkButton:
        return ctk.CTkButton(parent, text=text, width=width, height=34, font=self.fonts.body_bold, fg_color=theme.SECONDARY,
                             hover_color=theme.SECONDARY_HOVER, command=command,
                             image=icons.icon(icon_name, theme.TEXT, 16) if icon_name else None)

    def _run_background(self, work, kind: str) -> None:
        if self._busy:
            return
        self._busy = True

        def runner() -> None:
            try:
                result = work()
            except Exception as exc:
                result = exc
            self._events.put((kind, result))

        threading.Thread(target=runner, name=f"settings-{kind}", daemon=True).start()

    # ─── вкладки ───
    def _build_voice(self, tab) -> None:
        page = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        page.pack(fill="both", expand=True)
        row = self._section(page, "Микрофон", "Если Джарвис вас не слышит — выберите микрофон и нажмите «Проверить».")
        self._mic_values = self._microphones()
        current = self.cfg.get("voice.input_device")
        current_label = DEFAULT_MIC if current in (None, "") else str(current)
        if current_label not in self._mic_values:
            self._mic_values.append(current_label)
        self.mic_menu = self._option(row, self._mic_values, current_label, 420)
        self.mic_menu.pack(side="left")
        ctk.CTkButton(row, text="", width=34, height=34, fg_color=theme.SECONDARY, hover_color=theme.SECONDARY_HOVER,
                      image=icons.icon("refresh", theme.TEXT, 16), command=self._refresh_mics).pack(side="left", padx=8)

        row = self._section(page, "Чувствительность", "Цифровое усиление для тихого микрофона.")
        self.gain = ctk.CTkSlider(row, from_=1.0, to=5.0, number_of_steps=8, width=320, progress_color=theme.ACCENT_HOVER,
                                  button_color=theme.ACCENT, button_hover_color=theme.ACCENT_HOVER,
                                  command=lambda v: self.gain_label.configure(text=f"×{v:.1f}"))
        self.gain.set(float(self.cfg.get("voice.input_gain", 1.0) or 1.0))
        self.gain.pack(side="left")
        self.gain_label = ctk.CTkLabel(row, text=f"×{self.gain.get():.1f}", font=self.fonts.body_bold, text_color=theme.TEXT)
        self.gain_label.pack(side="left", padx=10)

        row = self._section(page, "Проверка микрофона",
                            "Нажмите и скажите, например: «Джарвис, проверка связи». Запись — 4 секунды.")
        self._button(row, "Проверить микрофон", self._test_mic, "mic").pack(side="left")
        self.mic_level = ctk.CTkProgressBar(row, width=220, height=10, progress_color=theme.ACCENT, fg_color=theme.BORDER)
        self.mic_level.set(0)
        self.mic_level.pack(side="left", padx=12)
        self.mic_result = ctk.CTkLabel(page, text="", font=self.fonts.small, text_color=theme.MUTED, anchor="w",
                                       justify="left", wraplength=580)
        self.mic_result.pack(fill="x", pady=(4, 0))

        row = self._section(page, "Голос Джарвиса")
        voices = list(getattr(self.app.speaker, "voices", []) or VOICES)
        self.voice_menu = self._option(row, voices, str(self.cfg.get("tts.speaker", "aidar")), 200)
        self.voice_menu.pack(side="left")
        self._button(row, "Прослушать", lambda: self.app.preview_voice(self.voice_menu.get()), "play", 150).pack(
            side="left", padx=8)

        row = self._section(page, "Слово-активатор", "Варианты через запятую — как их распознаёт Vosk (видно в строке «Слышу»).")
        self.wake_entry = self._entry(row, ", ".join(self.cfg.get("voice.wake_words", []) or []), 420)
        self.wake_entry.pack(side="left")
        row = self._section(page, "Звук")
        self.earcons = self._switch(row, "Короткий сигнал, когда Джарвис начинает слушать",
                                    bool(self.cfg.get("voice.earcons", True)))
        self.earcons.pack(side="left")

    def _build_pc(self, tab) -> None:
        page = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        page.pack(fill="both", expand=True)
        names = [t["name"] for t in self.app.registry.schemas()]
        row = self._section(page, f"Инструменты Джарвиса ({len(names)})",
                            "Модель управляет компьютером только через эти функции — каждое действие видно в журнале.")
        ctk.CTkLabel(row, text=", ".join(names), font=self.fonts.mono, text_color=theme.MUTED, anchor="w",
                     justify="left", wraplength=580).pack(fill="x")
        row = self._section(page, "Команды PowerShell",
                            "Позволяет выполнять любые задачи, для которых нет отдельного инструмента (создать "
                            "ярлык, узнать IP-адрес, переименовать файлы…). Каждую команду вы увидите целиком и "
                            "подтвердите кнопкой «Да, выполнить».")
        self.powershell = self._switch(row, "Разрешить run_powershell", bool(self.cfg.get("tools.allow_powershell", False)))
        self.powershell.pack(side="left")
        row = self._section(page, "Программирование",
                            "Джарвис пишет код в файлы (write_file — открываются в VS Code, Notepad++ или Блокноте) "
                            "и может запускать программы на Python. Каждый запуск вы подтверждаете, видя код целиком.")
        self.allow_code = self._switch(row, "Разрешить запуск кода (run_python)", bool(self.cfg.get("tools.allow_code", True)))
        self.allow_code.pack(side="left")
        row = self._section(page, "Проверка инструментов", "Безопасная проверка: дата, состояние системы, папки, громкость.")
        self._button(row, "Проверить инструменты", self._test_tools, "check").pack(side="left")
        self.tools_result = ctk.CTkTextbox(page, height=200, font=self.fonts.mono, fg_color=theme.INPUT,
                                           border_width=1, border_color=theme.BORDER, text_color=theme.TEXT, wrap="word")
        self.tools_result.pack(fill="x", pady=(8, 0))
        self.tools_result.configure(state="disabled")

    def _secret(self, parent, env_name: str, width: int = 250) -> ctk.CTkEntry:
        """Поле для ключа API: ввод скрыт, сохранённый ключ показан маской."""
        saved = mask_secret(os.environ.get(env_name))
        entry = ctk.CTkEntry(parent, width=width, height=34, font=self.fonts.body, fg_color=theme.INPUT,
                             border_color=theme.BORDER, text_color=theme.TEXT, show="•",
                             placeholder_text=f"ключ сохранён: {saved}" if saved else "вставьте ключ API",
                             placeholder_text_color=theme.STATUS_COLORS["ok"] if saved else theme.FAINT)
        self._secrets[env_name] = entry
        return entry

    def _row(self, page) -> ctk.CTkFrame:
        row = ctk.CTkFrame(page, fg_color="transparent")
        row.pack(fill="x", pady=(2, 2))
        return row

    def _build_models(self, tab) -> None:
        page = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        page.pack(fill="both", expand=True)
        row = self._section(page, "Ollama (локально, бесплатно)",
                            "Модель должна поддерживать инструменты, например qwen3:8b или qwen3:4b.")
        self.ollama_model = self._combo(row, ["qwen3:8b", "qwen3:4b", "qwen3:14b"], str(self.cfg.get("ollama.model", "")), 260)
        self.ollama_model.pack(side="left")
        self.ollama_host = self._entry(row, str(self.cfg.get("ollama.host", "")), 250)
        self.ollama_host.pack(side="left", padx=8)

        row = self._section(page, "Qwen (BazaarLink)",
                            "qwen/qwen3.7-flash:free — бесплатно, до 50 запросов в день. Ключ (вида sk-bl-…) — "
                            "на bazaarlink.ai в разделе API Keys; вставьте его справа (Ctrl+V).")
        self.qwen_model = self._combo(row, QWEN_MODELS, str(self.cfg.get("qwen.model", "")), 260)
        self.qwen_model.pack(side="left")
        self._secret(row, "BAZAARLINK_API_KEY").pack(side="left", padx=8)

        row = self._section(page, "GigaChat", "Ключ авторизации — developers.sber.ru/studio → GigaChat API → "
                                              "«Получить ключ».")
        self.giga_model = self._combo(row, GIGACHAT_MODELS, str(self.cfg.get("gigachat.model", "")), 260)
        self.giga_model.pack(side="left")
        self._secret(row, "GIGACHAT_CREDENTIALS").pack(side="left", padx=8)

        row = self._section(page, "Gemini", "Бесплатный ключ — aistudio.google.com/apikey. Прокси нужен, если Gemini "
                                            "недоступен в вашем регионе (например http://127.0.0.1:10809).")
        self.gemini_model = self._combo(row, GEMINI_MODELS, str(self.cfg.get("gemini.model", "")), 260)
        self.gemini_model.pack(side="left")
        self._secret(row, "GEMINI_API_KEY").pack(side="left", padx=8)
        row = self._row(page)
        ctk.CTkLabel(row, text="Прокси для Gemini:", font=self.fonts.small, text_color=theme.MUTED).pack(side="left")
        self.gemini_proxy = self._entry(row, str(self.cfg.get("gemini.proxy", "") or ""), 250)
        self.gemini_proxy.pack(side="left", padx=8)

        row = self._section(page, "Файлы программы", "Ключи хранятся в файле .env в папке программы.")
        self._button(row, "Открыть папку Джарвиса", lambda: open_in_explorer(ROOT_DIR), "folder", 230).pack(side="left")

    def _build_memory(self, tab) -> None:
        page = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        page.pack(fill="both", expand=True)
        services = self.app.services
        row = self._section(page, "Что Джарвис о вас помнит",
                            "По одному факту в строке. Можно дописать или удалить — изменения применятся по «Сохранить». "
                            "Голосом: «запомни, что…», «забудь…».")
        self.memory_box = ctk.CTkTextbox(page, height=130, font=self.fonts.body, fg_color=theme.INPUT, border_width=1,
                                         border_color=theme.BORDER, text_color=theme.TEXT, wrap="word")
        self._memory_text = "\n".join(services.memory.facts())
        self.memory_box.insert("1.0", self._memory_text)
        self.memory_box.pack(fill="x", pady=(4, 0))
        row = self._row(page)
        self.save_history = self._switch(row, "Помнить разговор после перезапуска",
                                         bool(self.cfg.get("memory.save_history", True)))
        self.save_history.pack(side="left")
        row = self._row(page)
        self.active_window = self._switch(row, "Учитывать активное окно («исправь это», «что тут»)",
                                          bool(self.cfg.get("context.active_window", True)))
        self.active_window.pack(side="left")

        row = self._section(page, "Сценарии, расписание и навыки",
                            "Сценарий — цепочка действий одной фразой («рабочий режим»), расписание — повтор по времени, "
                            "навык — то, чему Джарвис научился. Создаются голосом: «запомни как сценарий…», «каждый будний "
                            "день в 9 утра…», «сохрани это как навык».")
        lines = [f"Сценарий «{name}»: " + ", ".join(step["tool"] for step in item["steps"])
                 for name, item in services.routines.all().items()]
        lines += ["По расписанию " + services.schedule.describe(task) for task in services.schedule.all()]
        lines += [f"Навык «{skill.title or skill.name}»: {skill.description}" for skill in services.skills.all()]
        summary = ctk.CTkTextbox(page, height=110, font=self.fonts.small, fg_color=theme.INPUT, border_width=1,
                                 border_color=theme.BORDER, text_color=theme.TEXT, wrap="word")
        summary.insert("1.0", "\n".join(lines) or "Пока пусто.")
        summary.configure(state="disabled")
        summary.pack(fill="x", pady=(4, 0))
        row = self._row(page)
        from ..storage import data_folder

        self._button(row, "Папка с данными", lambda: open_in_explorer(data_folder(self.cfg)), "folder", 180).pack(side="left")

        row = self._section(page, "Джарвис сам предлагает помощь",
                            "Раз в несколько минут проверяет диск, батарею, память и процессор и, если что-то не так, "
                            "предлагает помочь. Не чаще двух раз в час, не в тихие часы и не в режиме «Не беспокоить».")
        self.proactive = self._switch(row, "Предлагать помощь", bool(self.cfg.get("proactive.enabled", True)))
        self.proactive.pack(side="left")
        row = self._row(page)
        self.briefing = self._switch(row, "Утренний брифинг (погода, дела, состояние ПК)",
                                     bool(self.cfg.get("proactive.morning_briefing", True)))
        self.briefing.pack(side="left")
        row = self._row(page)
        ctk.CTkLabel(row, text="Город для погоды:", font=self.fonts.small, text_color=theme.MUTED).pack(side="left")
        self.city = self._entry(row, str(self.cfg.get("briefing.city", "") or ""), 200)
        self.city.pack(side="left", padx=8)
        row = self._row(page)
        ctk.CTkLabel(row, text="Тихие часы:", font=self.fonts.small, text_color=theme.MUTED).pack(side="left")
        self.quiet_hours = self._entry(row, str(self.cfg.get("proactive.quiet_hours", "23:00-08:00")), 140)
        self.quiet_hours.pack(side="left", padx=8)
        self._button(row, "Брифинг сейчас", self.app.briefing_now, "play", 170).pack(side="left", padx=4)
        row = self._row(page)
        self.hud = self._switch(row, "Мини-индикатор поверх окон (HUD)", bool(self.cfg.get("ui.hud", True)))
        self.hud.pack(side="left")
        muted = self.app.proactive.muted_kinds()
        if muted:
            row = self._row(page)
            ctk.CTkLabel(row, text=f"Отключённые предложения: {len(muted)}", font=self.fonts.small,
                         text_color=theme.MUTED).pack(side="left")
            self._button(row, "Снова предлагать всё", self._unmute, "refresh", 200).pack(side="left", padx=8)

    def _unmute(self) -> None:
        self.app.proactive.unmute_all()
        self.saved.configure(text="Предложения снова включены", text_color=theme.STATUS_COLORS["ok"])

    def _build_diagnostics(self, tab) -> None:
        top = ctk.CTkFrame(tab, fg_color="transparent")
        top.pack(fill="x", pady=(6, 4))
        self._button(top, "Запустить диагностику", self._diagnose, "check", 220).pack(side="left")
        self._button(top, "Папка с логами", lambda: open_in_explorer(ROOT_DIR / "logs"), "folder", 170).pack(
            side="left", padx=8)
        ctk.CTkLabel(tab, text="Проверяет библиотеки, микрофон (скажите «Джарвис, проверка связи»), синтез речи, "
                               "модель и инструменты. Отчёт сохраняется в logs\\diagnostics.txt — его можно "
                               "приложить к вопросу.", font=self.fonts.small, text_color=theme.MUTED, anchor="w",
                     justify="left", wraplength=600).pack(fill="x")
        self.report = ctk.CTkTextbox(tab, font=self.fonts.mono, fg_color=theme.INPUT, border_width=1,
                                     border_color=theme.BORDER, text_color=theme.TEXT, wrap="word")
        self.report.pack(fill="both", expand=True, pady=(8, 4))
        self.report.configure(state="disabled")

    # ─── действия ───
    def _microphones(self) -> list[str]:
        return [DEFAULT_MIC] + [d["name"] for d in self.app.list_microphones()]

    def _refresh_mics(self) -> None:
        current = self.mic_menu.get()
        self._mic_values = self._microphones()
        if current not in self._mic_values:
            current = DEFAULT_MIC
        self.mic_menu.configure(values=self._mic_values)
        self.mic_menu.set(current)

    def _test_mic(self) -> None:
        self._apply_voice_now()
        self.mic_result.configure(text="Говорите… (4 секунды)", text_color=theme.ACCENT)
        levels = self._events

        def work():
            return self.app.listener.probe(4.0, on_level=lambda level: levels.put(("level", level)))

        self._run_background(work, "mic")

    def _test_tools(self) -> None:
        self._set_text(self.tools_result, "Проверяю…\n")
        self._run_background(self.app.self_test_tools, "tools")

    def _diagnose(self) -> None:
        self._set_text(self.report, "")
        lines = self._events
        self._run_background(lambda: self.app.run_diagnostics(lambda line: lines.put(("line", line))), "diagnostics")

    @staticmethod
    def _set_text(box: ctk.CTkTextbox, text: str, append: bool = False) -> None:
        box.configure(state="normal")
        if not append:
            box.delete("1.0", "end")
        box.insert("end", text)
        box.see("end")
        box.configure(state="disabled")

    def _poll(self) -> None:
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        while True:
            try:
                kind, value = self._events.get_nowait()
            except queue.Empty:
                break
            if kind == "level":
                self.mic_level.set(max(0.0, min(1.0, value)))
            elif kind == "line":
                self._set_text(self.report, value + "\n", append=True)
            elif kind == "mic":
                self._busy = False
                self.mic_level.set(0)
                self._show_mic_result(value)
            elif kind == "tools":
                self._busy = False
                text = f"Ошибка: {value}" if isinstance(value, Exception) else "\n".join(value)
                self._set_text(self.tools_result, text)
            elif kind == "diagnostics":
                self._busy = False
                if isinstance(value, Exception):
                    self._set_text(self.report, f"\nОшибка диагностики: {value}\n", append=True)
        self.after(100, self._poll)

    def _show_mic_result(self, result) -> None:
        if isinstance(result, Exception):
            message = getattr(result, "message", str(result))
            hint = getattr(result, "hint", "")
            self.mic_result.configure(text=f"✗ {message} {hint}".strip(), text_color=theme.ERROR_TEXT)
            return
        device = result.get("device") or "?"
        if result.get("no_audio"):
            text, color = f"✗ Микрофон «{device}» не передаёт звук.", theme.ERROR_TEXT
        elif result.get("silent"):
            text, color = f"✗ «{device}»: полная тишина. {PRIVACY_HINT}", theme.ERROR_TEXT
        else:
            level = result.get("rms_db", -120)
            loud = "громкость хорошая" if level > -45 else "очень тихо — увеличьте чувствительность"
            heard = result.get("text") or "ничего не распознано"
            text = f"✓ «{device}»: {loud} ({level:.0f} дБ). Распознано: «{heard}»"
            color = theme.TEXT if level > -45 else theme.HINT
        self.mic_result.configure(text=text, text_color=color)

    def _apply_voice_now(self) -> None:
        """Микрофон и чувствительность применяем сразу — чтобы проверка шла с ними."""
        changes = self._collect()
        voice_changes = {k: v for k, v in changes.items() if k in ("voice.input_device", "voice.input_gain")}
        if voice_changes:
            self.app.apply_settings(voice_changes)

    def _collect(self) -> dict:
        mic = self.mic_menu.get()
        wake = [w.strip().lower() for w in self.wake_entry.get().replace(";", ",").split(",") if w.strip()]
        values = {
            "voice.input_device": None if mic == DEFAULT_MIC else mic,
            "voice.input_gain": round(float(self.gain.get()), 1),
            "tts.speaker": self.voice_menu.get(),
            "voice.wake_words": wake or ["джарвис"],
            "voice.earcons": bool(self.earcons.get()),
            "tools.allow_powershell": bool(self.powershell.get()),
            "tools.allow_code": bool(self.allow_code.get()),
            "ollama.model": self.ollama_model.get().strip(),
            "ollama.host": self.ollama_host.get().strip(),
            "gigachat.model": self.giga_model.get().strip(),
            "gemini.model": self.gemini_model.get().strip(),
            "gemini.proxy": self.gemini_proxy.get().strip(),
            "qwen.model": self.qwen_model.get().strip(),
            "memory.save_history": bool(self.save_history.get()),
            "context.active_window": bool(self.active_window.get()),
            "proactive.enabled": bool(self.proactive.get()),
            "proactive.morning_briefing": bool(self.briefing.get()),
            "proactive.quiet_hours": self.quiet_hours.get().strip(),
            "briefing.city": self.city.get().strip(),
            "ui.hud": bool(self.hud.get()),
        }
        return {key: value for key, value in values.items() if value != self.cfg.get(key)}

    def _save(self) -> None:
        changes = self._collect()
        memory_text = self.memory_box.get("1.0", "end").strip()
        if memory_text != self._memory_text:
            self.app.services.memory.set_all(memory_text.splitlines())
            self._memory_text = memory_text
            changes["memory"] = True
        secrets = {name: entry.get().strip() for name, entry in self._secrets.items() if entry.get().strip()}
        if secrets:
            self.app.save_secrets(secrets)
            for name in secrets:  # поле очищаем, ключ показываем маской
                entry = self._secrets[name]
                entry.delete(0, "end")
                entry.configure(placeholder_text=f"ключ сохранён: {mask_secret(secrets[name])}",
                                placeholder_text_color=theme.STATUS_COLORS["ok"])
                self.focus_set()
        if changes:
            self.app.apply_settings({k: v for k, v in changes.items() if k != "memory"})
        total = len(changes) + len(secrets)
        if total:
            self.saved.configure(text=f"Сохранено: {total} изм.", text_color=theme.STATUS_COLORS["ok"])
        else:
            self.saved.configure(text="Изменений нет", text_color=theme.MUTED)
