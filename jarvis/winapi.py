"""Тонкие обёртки над WinAPI (ctypes): окна, фокус, ввод текста, папки, уведомления.

Модуль импортируется и на других ОС (для тестов), но функции работают только в Windows.
"""

from __future__ import annotations

import base64
import logging
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200

if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    ole32 = ctypes.WinDLL("ole32", use_last_error=True)

    ULONG_PTR = ctypes.c_size_t

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
    user32.SendInput.restype = wintypes.UINT
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindow.restype = wintypes.HWND
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ULONG_PTR]
    shell32.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE,
                                             ctypes.POINTER(ctypes.c_void_p)]
    shell32.SHGetKnownFolderPath.restype = ctypes.c_long
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.restype = wintypes.HGLOBAL

INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
VK_RETURN = 0x0D
VK_TAB = 0x09
WM_CLOSE = 0x0010
WM_SYSCOMMAND = 0x0112
SC_CLOSE = 0xF060
SW_RESTORE = 9
GW_OWNER = 4

SW_MAXIMIZE = 3
SW_MINIMIZE = 6
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PREV_TRACK = 0xB1
VK_MEDIA_PLAY_PAUSE = 0xB3


def _require_windows() -> None:
    if not IS_WINDOWS:
        raise OSError("Эта функция работает только в Windows.")


# ─── Известные папки (учитывают перенос в OneDrive) ─────────────────────

_KNOWN_FOLDER_IDS = {
    "Desktop": "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}",
    "Documents": "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}",
    "Downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
    "Pictures": "{33E28130-4E1E-4676-835A-98395C3BC3BB}",
    "Music": "{4BD8D571-6D19-48D3-BE97-422220080E43}",
    "Videos": "{18989B1D-99B5-455B-841C-AB7C74E4DDFC}",
}


def known_folder(name: str) -> Path:
    """Путь к «Документам», «Загрузкам» и т. п. — даже если они перенесены в OneDrive."""
    if name == "Home":
        return Path.home()
    if IS_WINDOWS and name in _KNOWN_FOLDER_IDS:
        guid = GUID.from_buffer_copy(uuid.UUID(_KNOWN_FOLDER_IDS[name]).bytes_le)
        pointer = ctypes.c_void_p()
        result = shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(pointer))
        try:
            if result == 0 and pointer.value:
                return Path(ctypes.wstring_at(pointer.value))
        finally:
            if pointer.value:
                ole32.CoTaskMemFree(pointer)
    return Path.home() / name


def short_path(path: str) -> str:
    """Короткий путь 8.3 (только ASCII) — для библиотек, не понимающих кириллицу в путях."""
    if not IS_WINDOWS or path.isascii():
        return path
    kernel32.GetShortPathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    kernel32.GetShortPathNameW.restype = wintypes.DWORD
    size = kernel32.GetShortPathNameW(path, None, 0)
    if not size:
        return path
    buffer = ctypes.create_unicode_buffer(size)
    return buffer.value if kernel32.GetShortPathNameW(path, buffer, size) else path


# ─── Окна и фокус ──────────────────────────────────────────────────────

def foreground_window() -> int:
    if not IS_WINDOWS:
        return 0
    return user32.GetForegroundWindow() or 0


def window_pid(hwnd: int) -> int:
    if not IS_WINDOWS or not hwnd:
        return 0
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def window_title(hwnd: int) -> str:
    if not IS_WINDOWS or not hwnd:
        return ""
    length = user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def window_class(hwnd: int) -> str:
    if not IS_WINDOWS or not hwnd:
        return ""
    buffer = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buffer, 256)
    return buffer.value


def is_window(hwnd: int) -> bool:
    return bool(IS_WINDOWS and hwnd and user32.IsWindow(hwnd))


def activate_window(hwnd: int, timeout: float = 1.0) -> bool:
    """Выводит окно на передний план. Возвращает True, если это удалось."""
    if not is_window(hwnd):
        return False
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    current_tid = kernel32.GetCurrentThreadId()
    foreground_tid = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
    target_tid = user32.GetWindowThreadProcessId(hwnd, None)
    attached = []
    for tid in {foreground_tid, target_tid} - {current_tid, 0}:
        if user32.AttachThreadInput(current_tid, tid, True):
            attached.append(tid)
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        for tid in attached:
            user32.AttachThreadInput(current_tid, tid, False)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if user32.GetForegroundWindow() == hwnd:
            time.sleep(0.1)  # даём окну принять фокус клавиатуры
            return True
        time.sleep(0.05)
    return user32.GetForegroundWindow() == hwnd


def top_windows(pids: set[int] | None = None, class_name: str | None = None) -> list[int]:
    """Видимые окна верхнего уровня (без владельца) заданных процессов/класса."""
    if not IS_WINDOWS:
        return []
    found: list[int] = []

    def callback(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd) and not user32.GetWindow(hwnd, GW_OWNER):
            if (pids is None or window_pid(hwnd) in pids) and (class_name is None or window_class(hwnd) == class_name):
                found.append(hwnd)
        return True

    user32.EnumWindows(WNDENUMPROC(callback), 0)
    return found


def close_window(hwnd: int) -> None:
    """Как нажатие «крестика»: приложение само спросит о несохранённых данных."""
    if is_window(hwnd):
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)


def system_close_window(hwnd: int) -> None:
    """То же, что Alt+F4 для конкретного окна (WM_SYSCOMMAND / SC_CLOSE)."""
    if is_window(hwnd):
        user32.PostMessageW(hwnd, WM_SYSCOMMAND, SC_CLOSE, 0)


def show_window(hwnd: int, command: int) -> bool:
    """Свернуть (SW_MINIMIZE), развернуть (SW_MAXIMIZE) или восстановить (SW_RESTORE) окно."""
    if not is_window(hwnd):
        return False
    user32.ShowWindow(hwnd, command)
    return True


# ─── Буфер обмена ─────────────────────────────────────────────────────

def _open_clipboard() -> None:
    for _ in range(20):  # буфер может быть ненадолго занят другой программой
        if user32.OpenClipboard(None):
            return
        time.sleep(0.05)
    raise OSError("буфер обмена занят другой программой")


def get_clipboard_text() -> str:
    _require_windows()
    _open_clipboard()
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return ""
        try:
            return ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def set_clipboard_text(text: str) -> None:
    _require_windows()
    data = (text.replace("\r\n", "\n").replace("\n", "\r\n") + "\0").encode("utf-16-le")
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not handle:
        raise OSError("не удалось выделить память для буфера обмена")
    pointer = kernel32.GlobalLock(handle)
    ctypes.memmove(pointer, data, len(data))
    kernel32.GlobalUnlock(handle)
    _open_clipboard()
    try:
        user32.EmptyClipboard()
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            kernel32.GlobalFree(handle)
            raise OSError(f"SetClipboardData не сработал (код {ctypes.get_last_error()})")
    finally:
        user32.CloseClipboard()


class FocusTracker:
    """Помнит последнее «чужое» активное окно — туда печатаем текст и шлём клавиши,
    даже если пользователь переключился в окно Джарвиса, чтобы написать команду."""

    def __init__(self) -> None:
        self._own_pid = os.getpid()
        self._last_external = 0

    def update(self) -> None:
        hwnd = foreground_window()
        if hwnd and window_pid(hwnd) != self._own_pid and window_title(hwnd):
            self._last_external = hwnd

    def target(self) -> int:
        hwnd = foreground_window()
        if hwnd and window_pid(hwnd) != self._own_pid:
            return hwnd
        return self._last_external if is_window(self._last_external) else 0

    def own_window_active(self) -> bool:
        hwnd = foreground_window()
        return bool(hwnd) and window_pid(hwnd) == self._own_pid

    def focus_target(self) -> int:
        """Если активно окно Джарвиса — переключается на последнее чужое окно."""
        target = self.target()
        if target and self.own_window_active():
            activate_window(target)
        return target


# ─── Клавиатура ────────────────────────────────────────────────────────

def _keyboard_input(vk: int = 0, scan: int = 0, flags: int = 0) -> "INPUT":
    item = INPUT()
    item.type = INPUT_KEYBOARD
    item.ki = KEYBDINPUT(vk, scan, flags, 0, 0)
    return item


def send_unicode_text(text: str, delay: float = 0.003) -> None:
    """Печатает любой текст (кириллица, эмодзи) независимо от раскладки клавиатуры."""
    _require_windows()
    for char in text.replace("\r\n", "\n"):
        if char == "\n":
            events = [_keyboard_input(VK_RETURN), _keyboard_input(VK_RETURN, flags=KEYEVENTF_KEYUP)]
        elif char == "\t":
            events = [_keyboard_input(VK_TAB), _keyboard_input(VK_TAB, flags=KEYEVENTF_KEYUP)]
        else:
            data = char.encode("utf-16-le")
            units = [int.from_bytes(data[i:i + 2], "little") for i in range(0, len(data), 2)]
            events = [_keyboard_input(scan=u, flags=KEYEVENTF_UNICODE) for u in units]
            events += [_keyboard_input(scan=u, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP) for u in units]
        array = (INPUT * len(events))(*events)
        if user32.SendInput(len(events), array, ctypes.sizeof(INPUT)) != len(events):
            raise OSError(f"SendInput не сработал (код {ctypes.get_last_error()})")
        if delay:
            time.sleep(delay)


def press_media_key(vk: int) -> None:
    _require_windows()
    user32.keybd_event(vk, 0, KEYEVENTF_EXTENDEDKEY, 0)
    user32.keybd_event(vk, 0, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP, 0)


# ─── Система ───────────────────────────────────────────────────────────

def lock_workstation() -> None:
    _require_windows()
    if not user32.LockWorkStation():
        raise OSError(f"LockWorkStation не сработал (код {ctypes.get_last_error()})")


def suspend() -> None:
    """Спящий режим (не гибернация)."""
    _require_windows()
    powrprof = ctypes.WinDLL("powrprof", use_last_error=True)
    powrprof.SetSuspendState.argtypes = [wintypes.BOOLEAN, wintypes.BOOLEAN, wintypes.BOOLEAN]
    if not powrprof.SetSuspendState(False, False, False):
        raise OSError(f"SetSuspendState не сработал (код {ctypes.get_last_error()})")


def run_powershell(script: str, timeout: float = 30) -> subprocess.CompletedProcess:
    """Выполняет скрипт Windows PowerShell, вывод — в UTF-8. Команда передаётся
    через -EncodedCommand, поэтому кавычки и спецсимволы не ломаются."""
    _require_windows()
    prefix = ("[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
              "$OutputEncoding=[System.Text.Encoding]::UTF8;$ProgressPreference='SilentlyContinue';")
    encoded = base64.b64encode((prefix + script).encode("utf-16-le")).decode("ascii")
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-EncodedCommand", encoded],
        capture_output=True, timeout=timeout, creationflags=CREATE_NO_WINDOW,
    )


def decode_output(data: bytes | None) -> str:
    if not data:
        return ""
    for encoding in ("utf-8", "cp866", "cp1251"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


_POWERSHELL_AUMID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"


def _xml_escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&apos;"))


def show_toast(title: str, message: str) -> None:
    """Всплывающее уведомление Windows 10/11 (в фоне, ошибки только в лог)."""
    if not IS_WINDOWS:
        return
    xml = ('<toast scenario="reminder"><visual><binding template="ToastGeneric">'
           f"<text>{_xml_escape(title)}</text><text>{_xml_escape(message)}</text>"
           '</binding></visual><actions><action content="OK" arguments="dismiss" activationType="system"/>'
           '</actions><audio src="ms-winsoundevent:Notification.Reminder"/></toast>')
    script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null;"
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null;"
        "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument;"
        f"$xml.LoadXml('{xml.replace(chr(39), chr(39) * 2)}');"
        "$toast = [Windows.UI.Notifications.ToastNotification]::new($xml);"
        f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{_POWERSHELL_AUMID}').Show($toast);"
    )

    def worker() -> None:
        try:
            result = run_powershell(script, timeout=20)
            if result.returncode != 0:
                log.warning("Уведомление не показано: %s", decode_output(result.stderr).strip())
        except Exception as exc:
            log.warning("Уведомление не показано: %s", exc)

    threading.Thread(target=worker, name="toast", daemon=True).start()


def message_beep() -> None:
    if IS_WINDOWS:
        try:
            import winsound
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
        except Exception:
            pass
