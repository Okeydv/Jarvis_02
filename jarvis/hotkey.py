"""Глобальная горячая клавиша через WinAPI RegisterHotKey.

Работает, даже когда окно свёрнуто, не требует прав администратора и не зависит
от раскладки клавиатуры (используются виртуальные коды клавиш).
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

from . import winapi

log = logging.getLogger(__name__)

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012
_MODIFIERS = {"ctrl": MOD_CONTROL, "control": MOD_CONTROL, "alt": MOD_ALT, "shift": MOD_SHIFT, "win": MOD_WIN}
_NAMED_VK = {
    "space": 0x20, "enter": 0x0D, "tab": 0x09, "esc": 0x1B, "backspace": 0x08, "insert": 0x2D,
    "delete": 0x2E, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22, "pause": 0x13,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    **{f"f{i}": 0x6F + i for i in range(1, 25)},
}


def parse_hotkey(text: str) -> tuple[int, int]:
    """«ctrl+alt+j» → (модификаторы, виртуальный код клавиши)."""
    modifiers, vk = 0, None
    for part in [p.strip().lower() for p in text.split("+") if p.strip()]:
        if part in _MODIFIERS:
            modifiers |= _MODIFIERS[part]
        elif part in _NAMED_VK:
            vk = _NAMED_VK[part]
        elif len(part) == 1 and part.isascii() and part.isalnum():
            vk = ord(part.upper())
        else:
            raise ValueError(f"неизвестная клавиша «{part}»")
    if vk is None:
        raise ValueError("не указана основная клавиша")
    return modifiers, vk


class GlobalHotkey:
    def __init__(self, combo: str, callback: Callable[[], None]):
        self.combo = combo
        self.callback = callback
        self.error = ""
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()

    def start(self) -> bool:
        """Регистрирует клавишу. False — не получилось (причина в self.error)."""
        if not winapi.IS_WINDOWS:
            self.error = "глобальные горячие клавиши работают только в Windows"
            return False
        try:
            modifiers, vk = parse_hotkey(self.combo)
        except ValueError as exc:
            self.error = f"неверная горячая клавиша «{self.combo}»: {exc}"
            return False
        self._thread = threading.Thread(target=self._run, args=(modifiers, vk), name="hotkey", daemon=True)
        self._thread.start()
        self._ready.wait(3)
        return not self.error

    def _run(self, modifiers: int, vk: int) -> None:
        import ctypes
        from ctypes import wintypes

        user32, kernel32 = winapi.user32, winapi.kernel32
        self._thread_id = kernel32.GetCurrentThreadId()
        if not user32.RegisterHotKey(None, 1, modifiers | MOD_NOREPEAT, vk):
            code = ctypes.get_last_error()
            self.error = (f"не удалось зарегистрировать {self.combo} (код {code}) — вероятно, сочетание занято "
                          "другой программой. Измените hotkey в config.yaml.")
            self._ready.set()
            return
        self._ready.set()
        message = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                if message.message == WM_HOTKEY:
                    try:
                        self.callback()
                    except Exception:
                        log.exception("Ошибка обработчика горячей клавиши")
        finally:
            user32.UnregisterHotKey(None, 1)

    def stop(self) -> None:
        if winapi.IS_WINDOWS and self._thread_id:
            winapi.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
