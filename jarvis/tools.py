"""Инструменты управления ПК, которые модель вызывает через function calling.

Каждый инструмент — функция с описанием и JSON-схемой параметров на русском.
Опасные действия (close_app, sleep_pc, shutdown_pc, restart_pc, run_powershell,
hotkey с Alt+F4) выполняются только после подтверждения пользователя: это проверяет
сам реестр (ToolRegistry.execute) до вызова функции, забыть подтверждение нельзя.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

import psutil

from . import winapi
from .text_utils import normalize, ru_plural, similarity, translit_ru_en

log = logging.getLogger(__name__)


class ToolError(Exception):
    """Понятная ошибка инструмента — её текст получает модель."""


@dataclass
class Confirmation:
    """Что именно пользователь должен подтвердить."""

    title: str
    text: str
    details: str = ""  # например, полная команда PowerShell
    data: Any = None   # подготовленные данные, которые получит инструмент


@dataclass
class ToolResult:
    status: str  # ok | error | cancelled
    text: str

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    func: Callable[..., str]
    confirm: Optional[Callable[..., Optional[Confirmation]]] = None
    available: Optional[Callable[[Any], bool]] = None
    # Действие, результат которого — готовый ответ пользователю («Громкость 30%»): после него
    # агент может не обращаться к модели второй раз (быстрее). Функция — решение по аргументам.
    quick: bool | Callable[[dict], bool] = False

    def schema(self) -> dict:
        return {"name": self.name, "description": self.description, "parameters": self.parameters}

    def is_quick(self, arguments: dict) -> bool:
        return bool(self.quick(arguments) if callable(self.quick) else self.quick)


TOOLS: dict[str, Tool] = {}


def tool(name: str, description: str, properties: dict | None = None, required: tuple = (),
         confirm: Callable | None = None, available: Callable | None = None,
         quick: bool | Callable[[dict], bool] = False):
    """Регистрирует функцию как инструмент."""

    def decorator(func: Callable[..., str]) -> Callable[..., str]:
        TOOLS[name] = Tool(
            name=name,
            description=description,
            parameters={"type": "object", "properties": properties or {}, "required": list(required)},
            func=func,
            confirm=confirm,
            available=available,
            quick=quick,
        )
        return func

    return decorator


class ToolServices:
    """Общие сервисы для инструментов: настройки, подтверждение, уведомления, фокус."""

    def __init__(self, config, confirm: Callable[[Confirmation], bool] | None = None,
                 notify: Callable[[str, str], None] | None = None,
                 focus: winapi.FocusTracker | None = None):
        self.config = config
        # Без обработчика подтверждения опасные действия запрещены.
        self.confirm = confirm or (lambda confirmation: False)
        self.notify = notify or (lambda title, text: None)
        self.focus = focus
        self.apps = AppCatalog(config.get("tools.apps", {}) or {})
        self.timers: list[threading.Timer] = []


class CallContext:
    def __init__(self, services: ToolServices, cancel: threading.Event | None):
        self.services = services
        self.config = services.config
        self.cancel = cancel or threading.Event()
        self.prepared: Any = None


class ToolRegistry:
    def __init__(self, services: ToolServices):
        self.services = services

    def available_tools(self) -> list[Tool]:
        return [t for t in TOOLS.values() if t.available is None or t.available(self.services.config)]

    def schemas(self) -> list[dict]:
        return [t.schema() for t in self.available_tools()]

    def execute(self, name: str, arguments: Any, cancel: threading.Event | None = None) -> ToolResult:
        item = TOOLS.get(name)
        if item is None:
            names = ", ".join(t.name for t in self.available_tools())
            return ToolResult("error", f"Ошибка: инструмента «{name}» не существует. Доступны только: {names}.")
        if item.available is not None and not item.available(self.services.config):
            return ToolResult("error", f"Ошибка: инструмент «{name}» отключён в настройках (config.yaml).")
        try:
            args = validate_arguments(item.parameters, arguments)
        except ToolError as exc:
            return ToolResult("error", f"Ошибка в аргументах {name}: {exc}")

        ctx = CallContext(self.services, cancel)
        try:
            if item.confirm is not None:
                confirmation = item.confirm(ctx, **args)
                if confirmation is not None:
                    if ctx.cancel.is_set() or not self.services.confirm(confirmation):
                        return ToolResult(
                            "cancelled",
                            "Действие отменено: пользователь не подтвердил его (или истекло время ожидания). "
                            "Не повторяй его без новой просьбы.",
                        )
                    ctx.prepared = confirmation.data
            if ctx.cancel.is_set():
                return ToolResult("cancelled", "Действие отменено пользователем.")
            return ToolResult("ok", str(item.func(ctx, **args)))
        except ToolError as exc:
            return ToolResult("error", f"Ошибка: {exc}")
        except Exception as exc:  # инструмент не должен ронять программу
            log.exception("Сбой инструмента %s", name)
            return ToolResult("error", f"Ошибка при выполнении {name}: {type(exc).__name__}: {exc}")


# ─── Проверка и приведение аргументов ──────────────────────────────────

_TRUE = {"true", "1", "yes", "y", "on", "да", "вкл", "включить", "истина"}
_FALSE = {"false", "0", "no", "n", "off", "нет", "выкл", "выключить", "ложь"}


def _coerce(value: Any, spec: dict, key: str) -> Any:
    kind = spec.get("type", "string")
    if kind == "string":
        if isinstance(value, (dict, list)):
            if key == "keys" and isinstance(value, list):
                value = "+".join(str(v) for v in value)
            else:
                value = json.dumps(value, ensure_ascii=False)
        value = str(value)
        if "enum" in spec:
            options = {str(o).lower(): o for o in spec["enum"]}
            if value.strip().lower() not in options:
                raise ToolError(f"«{key}» должен быть одним из: {', '.join(spec['enum'])}")
            value = options[value.strip().lower()]
        return value
    if kind in ("integer", "number"):
        try:
            number = float(str(value).replace(",", ".").strip().rstrip("%"))
        except ValueError:
            raise ToolError(f"«{key}» должен быть числом, получено {value!r}") from None
        if kind == "integer":
            number = int(round(number))
        if "minimum" in spec and number < spec["minimum"]:
            raise ToolError(f"«{key}» не может быть меньше {spec['minimum']}")
        if "maximum" in spec and number > spec["maximum"]:
            raise ToolError(f"«{key}» не может быть больше {spec['maximum']}")
        return number
    if kind == "boolean":
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
        raise ToolError(f"«{key}» должен быть true или false, получено {value!r}")
    return value


def validate_arguments(schema: dict, arguments: Any) -> dict:
    if arguments is None or arguments == "":
        arguments = {}
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            raise ToolError("аргументы должны быть JSON-объектом") from None
    if not isinstance(arguments, dict):
        raise ToolError("аргументы должны быть объектом с именованными параметрами")
    properties = schema.get("properties", {})
    result = {}
    for key, value in arguments.items():
        if key in properties and value is not None:
            result[key] = _coerce(value, properties[key], key)
    for key in schema.get("required", []):
        if key not in result or (isinstance(result[key], str) and not result[key].strip()):
            raise ToolError(f"не указан обязательный параметр «{key}»")
    return result


# ─── Каталог приложений (алиасы из config.yaml) ────────────────────────

_NAME_FILLERS = {"приложение", "программу", "программа", "игру", "игра", "мне", "пожалуйста", "exe", "app"}


def clean_app_name(name: str) -> str:
    words = [w for w in normalize(name).split() if w not in _NAME_FILLERS]
    return " ".join(words)


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if str(v).strip()]
    return [str(value)] if str(value).strip() else []


def _exe_name(target: str) -> str | None:
    target = target.strip()
    if target.startswith('"'):
        target = target[1:].split('"', 1)[0]
    base = re.split(r"[\\/]", target)[-1]
    return base if base.lower().endswith(".exe") else None


@dataclass
class AppEntry:
    title: str
    names: list[str]
    run: list[str]
    process: list[str] = field(default_factory=list)


class AppCatalog:
    def __init__(self, apps: dict):
        self.entries: list[AppEntry] = []
        for key, value in apps.items():
            raw_names = [n.strip() for n in re.split(r"[,|]", str(key)) if n.strip()]
            if not raw_names:
                continue
            if isinstance(value, dict):
                run, process = _as_list(value.get("run")), _as_list(value.get("process"))
            else:
                run, process = _as_list(value), []
            if not process:
                process = [exe for exe in (_exe_name(t) for t in run) if exe]
            names = [clean_app_name(n) for n in raw_names]
            self.entries.append(AppEntry(raw_names[0], [n for n in names if n], run, process))

    def find(self, name: str) -> AppEntry | None:
        query = clean_app_name(name)
        if not query:
            return None
        query_en = translit_ru_en(query)
        for entry in self.entries:
            if query in entry.names or query_en in entry.names:
                return entry
        best, best_score = None, 0.0
        for entry in self.entries:
            for candidate in entry.names:
                score = max(similarity(query, candidate), similarity(query_en, translit_ru_en(candidate)))
                if score > best_score:
                    best, best_score = entry, score
        return best if best_score >= 0.85 else None


# ─── Поиск в меню «Пуск» ───────────────────────────────────────────────

_SHORTCUT_EXTENSIONS = {".lnk", ".url", ".appref-ms"}
_SHORTCUT_JUNK = ("uninstall", "удален", "удалить", "деинстал", "readme", "help", "справка",
                  "документац", "license", "лиценз", "release notes", "website", "веб-сайт", "setup")
_cache_lock = threading.Lock()
_shortcut_cache: dict[str, Any] = {"time": 0.0, "items": []}
_start_apps_cache: dict[str, Any] = {"time": 0.0, "items": []}


def _shortcut_dirs() -> list[Path]:
    dirs = [
        Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
        Path(os.environ.get("APPDATA", str(Path.home()))) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
        winapi.known_folder("Desktop"),
        Path(os.environ.get("PUBLIC", r"C:\Users\Public")) / "Desktop",
    ]
    return [d for d in dirs if d.is_dir()]


def list_shortcuts() -> list[tuple[str, Path]]:
    with _cache_lock:
        if time.monotonic() - _shortcut_cache["time"] < 120 and _shortcut_cache["items"]:
            return _shortcut_cache["items"]
    items: list[tuple[str, Path]] = []
    for folder in _shortcut_dirs():
        try:
            for path in folder.rglob("*"):
                if path.suffix.lower() in _SHORTCUT_EXTENSIONS and not any(j in path.stem.lower() for j in _SHORTCUT_JUNK):
                    items.append((path.stem, path))
        except OSError as exc:  # папка без прав доступа и т. п.
            log.debug("Не удалось просмотреть %s: %s", folder, exc)
    with _cache_lock:
        _shortcut_cache.update(time=time.monotonic(), items=items)
    return items


def list_start_apps() -> list[tuple[str, str]]:
    """Приложения меню «Пуск», включая Microsoft Store (Get-StartApps)."""
    if not winapi.IS_WINDOWS:
        return []
    with _cache_lock:
        if time.monotonic() - _start_apps_cache["time"] < 300 and _start_apps_cache["items"]:
            return _start_apps_cache["items"]
    items: list[tuple[str, str]] = []
    try:
        result = winapi.run_powershell("Get-StartApps | Select-Object Name,AppID | ConvertTo-Json -Compress", timeout=20)
        data = json.loads(winapi.decode_output(result.stdout) or "[]")
        if isinstance(data, dict):
            data = [data]
        items = [(d["Name"], d["AppID"]) for d in data if d.get("Name") and d.get("AppID")]
    except Exception as exc:
        log.warning("Get-StartApps не сработал: %s", exc)
    with _cache_lock:
        _start_apps_cache.update(time=time.monotonic(), items=items)
    return items


def app_name_score(query: str, candidate: str) -> float:
    candidate = normalize(candidate)
    best = 0.0
    for variant in {query, translit_ru_en(query)}:
        if not variant:
            continue
        if variant == candidate:
            return 1.0
        if re.search(rf"(^|\s){re.escape(variant)}(\s|$)", candidate):
            best = max(best, 0.92)
        elif len(variant) >= 4 and candidate.startswith(variant):
            best = max(best, 0.86)
        best = max(best, similarity(variant, candidate))
        for word in candidate.split():
            best = max(best, similarity(variant, word) * 0.95)
    return best


def find_in_start_menu(name: str, extra_names: list[str] | None = None) -> tuple[str, str, str] | None:
    """Ищет приложение: (название, вид «lnk»/«appx», путь или AppID)."""
    queries = [q for q in {clean_app_name(name), *(extra_names or [])} if q]
    if not queries:
        return None
    best: tuple[float, int, tuple[str, str, str]] | None = None
    candidates = [(title, "lnk", str(path)) for title, path in list_shortcuts()]
    for pass_items in (candidates, None):
        if pass_items is None:
            if best and best[0] >= 0.9:
                break
            pass_items = [(title, "appx", app_id) for title, app_id in list_start_apps()]
        for title, kind, target in pass_items:
            score = max(app_name_score(q, title) for q in queries)
            if score >= 0.8 and (best is None or (score, -len(title)) > (best[0], best[1])):
                best = (score, -len(title), (title, kind, target))
    return best[2] if best else None


def _launch_start_item(item: tuple[str, str, str]) -> None:
    title, kind, target = item
    if kind == "appx":
        subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{target}"], creationflags=winapi.CREATE_NO_WINDOW)
    else:
        os.startfile(target)  # type: ignore[attr-defined]


# ─── Запуск ────────────────────────────────────────────────────────────

class LaunchError(Exception):
    pass


def default_browser_exe() -> str | None:
    if not winapi.IS_WINDOWS:
        return None
    import winreg

    base = r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations"
    command = ""
    for scheme in ("https", "http"):
        for choice in ("UserChoiceLatest", "UserChoice"):  # в новых сборках Windows 11 — UserChoiceLatest
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, rf"{base}\{scheme}\{choice}") as key:
                    prog_id = winreg.QueryValueEx(key, "ProgId")[0]
                with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, rf"{prog_id}\shell\open\command") as key:
                    command = winreg.QueryValueEx(key, None)[0]
                break
            except OSError:
                continue
        if command:
            break
    if not command:
        return None
    command = command.strip()
    exe = command[1:].split('"', 1)[0] if command.startswith('"') else command.split(" ", 1)[0]
    return exe if exe and os.path.exists(exe) else None


def _protocol_registered(scheme: str) -> bool:
    if not winapi.IS_WINDOWS:
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, scheme):
            return True
    except OSError:
        return False


def _search_home(config) -> str:
    parsed = urllib.parse.urlparse(config.get("tools.search_url", "https://www.google.com/search?q={query}"))
    return f"{parsed.scheme or 'https'}://{parsed.netloc or 'www.google.com'}"


def launch_target(target: str, config) -> str:
    """Запускает одну цель из алиаса. Возвращает описание, при неудаче — LaunchError."""
    raw = target.strip()
    if raw == "@browser":
        exe = default_browser_exe()
        if exe:
            subprocess.Popen([exe], creationflags=winapi.DETACHED_PROCESS | winapi.CREATE_NEW_PROCESS_GROUP)
            return os.path.basename(exe)
        webbrowser.open(_search_home(config), new=2)
        return "браузер по умолчанию"
    if raw.lower().startswith("start:"):
        item = find_in_start_menu(raw[6:])
        if not item:
            raise LaunchError(f"в меню «Пуск» нет «{raw[6:]}»")
        _launch_start_item(item)
        return f"ярлык «{item[0]}»"

    expanded = os.path.expandvars(raw)
    if expanded.startswith('"'):
        exe, _, args = expanded[1:].partition('"')
        if not os.path.exists(exe):
            raise LaunchError(f"нет файла {exe}")
        subprocess.Popen(f'"{exe}" {args.strip()}', creationflags=winapi.DETACHED_PROCESS | winapi.CREATE_NEW_PROCESS_GROUP)
        return os.path.basename(exe)
    scheme = re.match(r"^([a-zA-Z][a-zA-Z0-9+.\-]*):", expanded)
    if scheme and not re.match(r"^[a-zA-Z]:[\\/]", expanded):
        if scheme.group(1).lower() in ("http", "https"):
            webbrowser.open(expanded, new=2)
            return expanded
        if not _protocol_registered(scheme.group(1)):
            raise LaunchError(f"протокол {scheme.group(1)}: не зарегистрирован")
        os.startfile(expanded)  # type: ignore[attr-defined]
        return expanded
    if "\\" in expanded or "/" in expanded:
        if not os.path.exists(expanded):
            raise LaunchError(f"нет файла {expanded}")
        os.startfile(expanded)  # type: ignore[attr-defined]
        return os.path.basename(expanded)
    found = shutil.which(expanded)
    try:
        if found and found.lower().endswith((".cmd", ".bat")):
            subprocess.Popen([found], creationflags=winapi.CREATE_NO_WINDOW)
        else:
            os.startfile(found or expanded)  # type: ignore[attr-defined]
    except OSError as exc:
        raise LaunchError(f"{expanded}: {exc.strerror or exc}") from None
    return expanded


# ═══ Инструменты ═════════════════════════════════════════════════════

@tool(
    "open_app",
    "Открыть (запустить) приложение на компьютере по названию: «браузер», «блокнот», «калькулятор», "
    "«проводник», «телеграм», «Word» и т. п. Если программы нет в списке известных, ищет её в меню «Пуск».",
    {"name": {"type": "string", "description": "Название приложения, например «блокнот» или «Telegram»"}},
    required=("name",),
    quick=True,
)
def open_app(ctx: CallContext, name: str) -> str:
    if not winapi.IS_WINDOWS:
        raise ToolError("запуск приложений доступен только в Windows")
    entry = ctx.services.apps.find(name)
    errors: list[str] = []
    if entry:
        for target in entry.run:
            try:
                how = launch_target(target, ctx.config)
                return f"Запущено «{entry.title}» ({how})."
            except (LaunchError, OSError) as exc:
                errors.append(str(exc))
    item = find_in_start_menu(name, entry.names if entry else None)
    if item:
        _launch_start_item(item)
        return f"Запущено «{item[0]}» из меню «Пуск»."
    details = f" Попытки: {'; '.join(errors)}." if errors else ""
    raise ToolError(f"не нашёл приложение «{name}» ни в списке известных, ни в меню «Пуск».{details}")


_PROTECTED_PROCESSES = {
    "system", "registry", "idle", "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe",
    "lsass.exe", "lsaiso.exe", "svchost.exe", "dwm.exe", "fontdrvhost.exe", "sihost.exe", "ctfmon.exe",
    "memory compression", "securityhealthservice.exe", "msmpeng.exe", "audiodg.exe", "conhost.exe",
    "runtimebroker.exe", "taskhostw.exe", "startmenuexperiencehost.exe", "shellexperiencehost.exe",
    "searchhost.exe", "searchui.exe", "textinputhost.exe", "lockapp.exe", "spoolsv.exe", "wudfhost.exe",
    "dllhost.exe", "smartscreen.exe", "applicationframehost.exe",
}


def _own_process_tree() -> set[int]:
    pids = {os.getpid()}
    try:
        pids |= {p.pid for p in psutil.Process().parents()}
    except psutil.Error:
        pass
    return pids


def find_app_processes(services: ToolServices, name: str) -> tuple[str, list[psutil.Process], bool]:
    """Процессы приложения: (название, процессы, это Проводник)."""
    entry = services.apps.find(name)
    title = entry.title if entry else name.strip()
    wanted = {p.lower() for p in entry.process} if entry else set()
    query = clean_app_name(name).replace(" ", "")
    if not wanted:
        for variant in {query, translit_ru_en(query)}:
            if variant:
                wanted.add(variant if variant.endswith(".exe") else variant + ".exe")
    if "explorer.exe" in wanted:
        return title, [], True

    own = _own_process_tree()
    processes = [p for p in psutil.process_iter(["pid", "name"])
                 if p.pid not in own and (p.info.get("name") or "").lower() not in _PROTECTED_PROCESSES]
    matched = [p for p in processes if (p.info.get("name") or "").lower() in wanted]
    if not matched and not entry:
        variant = translit_ru_en(query)
        if len(variant) >= 3:
            for proc in processes:
                stem = (proc.info.get("name") or "").lower().removesuffix(".exe")
                if stem.startswith(variant) or similarity(variant, stem) >= 0.85:
                    matched.append(proc)
    return title, matched, False


def _confirm_close_app(ctx: CallContext, name: str, force: bool = False) -> Confirmation:
    title, processes, explorer = find_app_processes(ctx.services, name)
    if explorer:
        windows = winapi.top_windows(class_name="CabinetWClass")
        if not windows:
            raise ToolError("окна Проводника не открыты")
        return Confirmation("Закрыть окна Проводника",
                            f"Закрыть все окна Проводника ({len(windows)} шт.). Рабочий стол и панель задач не пострадают.",
                            data=("explorer", title, windows))
    if not processes:
        raise ToolError(f"приложение «{title}» не запущено (процесс не найден)")
    names = ", ".join(sorted({p.info.get("name") or "?" for p in processes}))
    count = f"{len(processes)} {ru_plural(len(processes), ('процесс', 'процесса', 'процессов'))}"
    if force:
        text = f"ПРИНУДИТЕЛЬНО завершить «{title}»: {count} ({names}). Несохранённые данные будут потеряны."
    else:
        text = f"Закрыть приложение «{title}»: {count} ({names}). Программа может спросить о сохранении данных."
    return Confirmation(f"Закрыть «{title}»", text, data=("processes", title, processes))


@tool(
    "close_app",
    "Закрыть запущенное приложение (завершить его процессы). Требует подтверждения пользователя. "
    "force=true — принудительно, без сохранения данных (только если обычное закрытие не помогло).",
    {
        "name": {"type": "string", "description": "Название приложения или процесса, например «блокнот», «chrome»"},
        "force": {"type": "boolean", "description": "Принудительно завершить процессы (по умолчанию false)"},
    },
    required=("name",),
    confirm=_confirm_close_app,
    quick=True,
)
def close_app(ctx: CallContext, name: str, force: bool = False) -> str:
    kind, title, payload = ctx.prepared
    if kind == "explorer":
        for hwnd in payload:
            winapi.close_window(hwnd)
        return f"Закрыто окон Проводника: {len(payload)}."
    processes: list[psutil.Process] = payload
    if force:
        denied = 0
        for proc in processes:
            try:
                proc.kill()
            except psutil.NoSuchProcess:
                pass
            except psutil.AccessDenied:
                denied += 1
        _gone, alive = psutil.wait_procs(processes, timeout=3)
        if not alive:
            return f"«{title}» принудительно завершено."
        reason = " (нет прав: процесс запущен от администратора)" if denied else ""
        return f"Не удалось завершить {len(alive)} из {len(processes)} процессов «{title}»{reason}."

    windows = winapi.top_windows({p.pid for p in processes})
    if not windows:
        return (f"У «{title}» нет открытых окон — вероятно, приложение свёрнуто в трей. "
                "Полностью завершить его можно с force=true (снова с подтверждением).")
    for hwnd in windows:
        winapi.close_window(hwnd)
    _gone, alive = psutil.wait_procs(processes, timeout=5)
    if not alive:
        return f"«{title}» закрыто."
    if winapi.top_windows({p.pid for p in alive}):
        return f"Окно «{title}» не закрылось — возможно, приложение спрашивает, сохранить ли изменения."
    return (f"Окна «{title}» закрыты, но {len(alive)} фоновых процессов ещё работают. "
            "Полностью завершить можно с force=true.")


@tool(
    "open_url",
    "Открыть сайт (веб-адрес) в браузере по умолчанию.",
    {"url": {"type": "string", "description": "Адрес, например https://ya.ru или youtube.com"}},
    required=("url",),
    quick=True,
)
def open_url(ctx: CallContext, url: str) -> str:
    url = url.strip()
    if not re.match(r"^https?://", url, re.IGNORECASE):
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:", url) and not re.match(r"^[\w.-]+:\d+", url):
            raise ToolError("можно открывать только адреса http и https")
        url = "https://" + url
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").strip(".")
    if not host or " " in parsed.netloc or ("." not in host and host != "localhost"):
        raise ToolError(f"некорректный адрес: {url}")
    webbrowser.open(url, new=2)
    return f"Открыл {url} в браузере."


@tool(
    "web_search",
    "Найти информацию в интернете: открывает страницу поиска в браузере по умолчанию.",
    {"query": {"type": "string", "description": "Поисковый запрос"}},
    required=("query",),
    quick=True,
)
def web_search(ctx: CallContext, query: str) -> str:
    template = ctx.config.get("tools.search_url", "https://www.google.com/search?q={query}")
    url = template.replace("{query}", urllib.parse.quote_plus(query.strip()))
    webbrowser.open(url, new=2)
    return f"Открыл поиск «{query.strip()}» в браузере."


_KNOWN_FOLDERS = {
    "загрузки": "Downloads", "downloads": "Downloads", "скачанное": "Downloads", "закачки": "Downloads",
    "документы": "Documents", "мои документы": "Documents", "documents": "Documents",
    "рабочий стол": "Desktop", "desktop": "Desktop",
    "изображения": "Pictures", "картинки": "Pictures", "фото": "Pictures", "фотографии": "Pictures",
    "pictures": "Pictures",
    "музыка": "Music", "music": "Music",
    "видео": "Videos", "videos": "Videos",
    "домашняя папка": "Home", "папка пользователя": "Home", "home": "Home",
}
_SHELL_FOLDERS = {
    "этот компьютер": "shell:MyComputerFolder", "мой компьютер": "shell:MyComputerFolder",
    "компьютер": "shell:MyComputerFolder", "корзина": "shell:RecycleBinFolder",
    "автозагрузка": "shell:Startup", "шрифты": "shell:Fonts",
}


def resolve_folder(path: str) -> Path | str:
    """«Загрузки», «Документы\\Jarvis», «C:\\Games» → путь (или shell:-адрес)."""
    text = path.strip().strip('"').strip("«»")
    key = normalize(text)
    if key in _SHELL_FOLDERS:
        return _SHELL_FOLDERS[key]
    if key in _KNOWN_FOLDERS:
        return winapi.known_folder(_KNOWN_FOLDERS[key])
    parts = re.split(r"[\\/]+", text, maxsplit=1)
    head = normalize(parts[0])
    if head in _KNOWN_FOLDERS and len(parts) == 2:
        return winapi.known_folder(_KNOWN_FOLDERS[head]) / parts[1]
    return Path(os.path.expanduser(os.path.expandvars(text)))


@tool(
    "open_folder",
    "Открыть папку в Проводнике. Понимает «Загрузки», «Документы», «Рабочий стол», «Изображения», "
    "«Музыка», «Видео», «Этот компьютер», «Корзина», вложенные пути вроде «Документы\\Jarvis» и полные пути.",
    {"path": {"type": "string", "description": "Название известной папки или полный путь"}},
    required=("path",),
    quick=True,
)
def open_folder(ctx: CallContext, path: str) -> str:
    target = resolve_folder(path)
    if isinstance(target, str):
        subprocess.Popen(["explorer.exe", target], creationflags=winapi.CREATE_NO_WINDOW)
        return f"Открыл «{path}»."
    if target.is_dir():
        os.startfile(str(target))  # type: ignore[attr-defined]
        return f"Открыл папку {target}."
    if target.is_file():
        subprocess.Popen(["explorer.exe", "/select,", str(target)])
        return f"Открыл папку с файлом {target}."
    raise ToolError(f"папка не найдена: {target}")


def _folder_path(path: str) -> Path:
    """Путь папки для создания/просмотра; просто имя — на рабочем столе."""
    target = resolve_folder(path)
    if isinstance(target, str):
        raise ToolError(f"«{path}» — системная папка, её можно только открыть")
    if not target.is_absolute():
        target = winapi.known_folder("Desktop") / target
    return target


@tool(
    "create_folder",
    "Создать папку. Понимает «Рабочий стол\\Проекты», «Документы\\Отчёты» или полный путь; "
    "если указано только имя — папка создаётся на рабочем столе.",
    {"path": {"type": "string", "description": "Путь или имя новой папки"}},
    required=("path",),
    quick=True,
)
def create_folder(ctx: CallContext, path: str) -> str:
    target = _folder_path(path)
    if target.is_dir():
        return f"Папка уже существует: {target}"
    target.mkdir(parents=True)
    return f"Папка создана: {target}"


@tool(
    "list_folder",
    "Показать, что лежит в папке (имена папок и файлов). Понимает «Загрузки», «Документы», "
    "«Рабочий стол», вложенные и полные пути.",
    {"path": {"type": "string", "description": "Название известной папки или полный путь"}},
    required=("path",),
)
def list_folder(ctx: CallContext, path: str) -> str:
    target = _folder_path(path)
    if not target.is_dir():
        raise ToolError(f"папка не найдена: {target}")
    entries = sorted(target.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
    entries = [e for e in entries if not e.name.startswith(".") and e.name.lower() != "desktop.ini"]
    if not entries:
        return f"Папка {target} пуста."
    shown = [f"{e.name}{'/' if e.is_dir() else ''}" for e in entries[:40]]
    more = f" … и ещё {len(entries) - 40}" if len(entries) > 40 else ""
    return f"В папке {target} ({len(entries)} шт.): " + ", ".join(shown) + more


_com_state = threading.local()


def _endpoint_volume():
    """Интерфейс громкости динамиков по умолчанию (pycaw, поддержка старого и нового API)."""
    import comtypes

    if not getattr(_com_state, "ready", False):
        try:
            comtypes.CoInitialize()
        except OSError:
            pass  # COM уже инициализирован в этом потоке
        _com_state.ready = True
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

    try:
        speakers = AudioUtilities.GetSpeakers()
        volume = getattr(speakers, "EndpointVolume", None)
        if volume is not None:
            return volume
        from ctypes import POINTER, cast

        interface = speakers.Activate(IAudioEndpointVolume._iid_, comtypes.CLSCTX_ALL, None)
        return cast(interface, POINTER(IAudioEndpointVolume))
    except Exception as exc:
        raise ToolError(f"не найдено устройство вывода звука (динамики или наушники): {exc}") from None


@tool(
    "set_volume",
    "Установить общую громкость звука компьютера в процентах.",
    {"level": {"type": "integer", "description": "Громкость от 0 до 100", "minimum": 0, "maximum": 100}},
    required=("level",),
    quick=True,
)
def set_volume(ctx: CallContext, level: int) -> str:
    volume = _endpoint_volume()
    volume.SetMasterVolumeLevelScalar(level / 100.0, None)
    if level > 0 and volume.GetMute():
        volume.SetMute(0, None)
    return f"Громкость установлена на {level}%."


@tool(
    "change_volume",
    "Сделать громче или тише на указанное число процентов (например +10 или −20). "
    "Используй для просьб «погромче», «потише».",
    {"delta": {"type": "integer", "description": "Изменение громкости в процентах, от −100 до 100",
               "minimum": -100, "maximum": 100}},
    required=("delta",),
    quick=True,
)
def change_volume(ctx: CallContext, delta: int) -> str:
    volume = _endpoint_volume()
    current = round(volume.GetMasterVolumeLevelScalar() * 100)
    level = max(0, min(100, current + delta))
    volume.SetMasterVolumeLevelScalar(level / 100.0, None)
    if level > 0 and volume.GetMute():
        volume.SetMute(0, None)
    return f"Громкость: было {current}%, стало {level}%."


@tool(
    "mute",
    "Выключить или включить звук компьютера. on=true — выключить звук (режим «без звука»), "
    "on=false — снова включить звук.",
    {"on": {"type": "boolean", "description": "true — выключить звук, false — включить"}},
    required=("on",),
    quick=True,
)
def mute(ctx: CallContext, on: bool) -> str:
    _endpoint_volume().SetMute(1 if on else 0, None)
    return "Звук выключен." if on else "Звук включён."


@tool(
    "set_brightness",
    "Установить яркость экрана в процентах (работает на ноутбуках и мониторах с поддержкой управления яркостью).",
    {"level": {"type": "integer", "description": "Яркость от 0 до 100", "minimum": 0, "maximum": 100}},
    required=("level",),
    quick=True,
)
def set_brightness(ctx: CallContext, level: int) -> str:
    winapi._require_windows()
    script = ("Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods -ErrorAction Stop | "
              f"Invoke-CimMethod -MethodName WmiSetBrightness -Arguments @{{Timeout=1; Brightness={level}}} "
              "-ErrorAction Stop | Out-Null")
    result = winapi.run_powershell(script, timeout=20)
    if result.returncode != 0:
        raise ToolError("яркость этого экрана нельзя изменить программно (обычно это получается только на "
                        "ноутбуках). Можно открыть параметры экрана: open_settings(«экран»).")
    return f"Яркость экрана: {level}%."


_SETTINGS_PAGES = {
    "главная": "ms-settings:", "параметры": "ms-settings:", "настройки": "ms-settings:",
    "экран": "ms-settings:display", "дисплей": "ms-settings:display", "яркость": "ms-settings:display",
    "ночной свет": "ms-settings:nightlight",
    "звук": "ms-settings:sound", "звуковые устройства": "ms-settings:sound-devices",
    "микрофон": "ms-settings:privacy-microphone", "доступ к микрофону": "ms-settings:privacy-microphone",
    "камера": "ms-settings:privacy-webcam",
    "bluetooth": "ms-settings:bluetooth", "блютуз": "ms-settings:bluetooth", "устройства": "ms-settings:bluetooth",
    "wi fi": "ms-settings:network-wifi", "wifi": "ms-settings:network-wifi", "вай фай": "ms-settings:network-wifi",
    "сеть": "ms-settings:network-status", "интернет": "ms-settings:network-status", "vpn": "ms-settings:network-vpn",
    "приложения": "ms-settings:appsfeatures", "автозагрузка": "ms-settings:startupapps",
    "приложения по умолчанию": "ms-settings:defaultapps",
    "питание": "ms-settings:powersleep", "электропитание": "ms-settings:powersleep", "батарея": "ms-settings:batterysaver",
    "обновления": "ms-settings:windowsupdate", "обновление windows": "ms-settings:windowsupdate",
    "персонализация": "ms-settings:personalization", "обои": "ms-settings:personalization-background",
    "фон": "ms-settings:personalization-background", "темы": "ms-settings:themes", "цвета": "ms-settings:personalization-colors",
    "уведомления": "ms-settings:notifications", "дата и время": "ms-settings:dateandtime", "время": "ms-settings:dateandtime",
    "язык": "ms-settings:regionlanguage", "клавиатура": "ms-settings:typing", "мышь": "ms-settings:mousetouchpad",
    "сенсорная панель": "ms-settings:devices-touchpad", "принтеры": "ms-settings:printers",
    "память": "ms-settings:storagesense", "хранилище": "ms-settings:storagesense", "диск": "ms-settings:storagesense",
    "учетные записи": "ms-settings:yourinfo", "конфиденциальность": "ms-settings:privacy",
    "о системе": "ms-settings:about", "система": "ms-settings:about",
    "специальные возможности": "ms-settings:easeofaccess", "игровой режим": "ms-settings:gaming-gamemode",
}


@tool(
    "open_settings",
    "Открыть раздел параметров Windows: «экран», «звук», «bluetooth», «wi-fi», «сеть», «приложения», "
    "«автозагрузка», «питание», «обновления», «обои», «уведомления», «дата и время», «язык», «мышь», "
    "«принтеры», «память», «микрофон», «о системе» и т. п.",
    {"page": {"type": "string", "description": "Название раздела параметров"}},
    required=("page",),
    quick=True,
)
def open_settings(ctx: CallContext, page: str) -> str:
    winapi._require_windows()
    key = normalize(page)
    uri = page.strip() if page.strip().lower().startswith("ms-settings:") else _SETTINGS_PAGES.get(key)
    if uri is None:
        best = max(_SETTINGS_PAGES, key=lambda name: similarity(key, name))
        uri = _SETTINGS_PAGES[best] if similarity(key, best) >= 0.75 else "ms-settings:"
    os.startfile(uri)  # type: ignore[attr-defined]
    return f"Открыл параметры Windows ({uri})."


_MEDIA_KEYS = {
    "play_pause": (winapi.VK_MEDIA_PLAY_PAUSE, "Пауза/воспроизведение"),
    "next": (winapi.VK_MEDIA_NEXT_TRACK, "Следующий трек"),
    "previous": (winapi.VK_MEDIA_PREV_TRACK, "Предыдущий трек"),
}


@tool(
    "media",
    "Управление музыкой и видео (медиаклавиши): пауза/продолжить, следующий или предыдущий трек.",
    {"action": {"type": "string", "enum": ["play_pause", "next", "previous"],
                "description": "play_pause — пауза/продолжить, next — следующий, previous — предыдущий"}},
    required=("action",),
    quick=True,
)
def media(ctx: CallContext, action: str) -> str:
    vk, label = _MEDIA_KEYS[action]
    winapi.press_media_key(vk)
    return f"{label}: нажато."


def _wait_for_external_window(ctx: CallContext, timeout: float = 2.0) -> int:
    focus = ctx.services.focus
    if focus is None:
        return winapi.foreground_window()
    deadline = time.monotonic() + timeout
    while True:
        target = focus.focus_target()
        if target and not focus.own_window_active():
            return target
        if time.monotonic() > deadline:
            return 0
        time.sleep(0.2)


@tool(
    "type_text",
    "Напечатать текст в активном окне, как с клавиатуры (работает с русским и английским текстом).",
    {"text": {"type": "string", "description": "Текст для ввода"}},
    required=("text",),
    quick=True,
)
def type_text(ctx: CallContext, text: str) -> str:
    if len(text) > 5000:
        raise ToolError("слишком длинный текст (больше 5000 символов)")
    target = _wait_for_external_window(ctx)
    if not target:
        raise ToolError("не нашёл окно, куда печатать: откройте нужное окно (например, Блокнот) и повторите")
    winapi.send_unicode_text(text)
    return f"Напечатано {len(text)} символов в окне «{winapi.window_title(target)}»."


_KEY_SYNONYMS = {
    "control": "ctrl", "ctl": "ctrl", "контрол": "ctrl", "ктрл": "ctrl",
    "альт": "alt", "option": "alt",
    "шифт": "shift",
    "windows": "win", "виндоус": "win", "вин": "win", "super": "win", "cmd": "win", "command": "win",
    "meta": "win", "пуск": "win",
    "return": "enter", "энтер": "enter", "ввод": "enter",
    "escape": "esc", "эскейп": "esc",
    "таб": "tab",
    "spacebar": "space", "пробел": "space",
    "бэкспейс": "backspace",
    "del": "delete", "делит": "delete",
    "ins": "insert",
    "pgup": "pageup", "page_up": "pageup", "pgdn": "pagedown", "page_down": "pagedown",
    "вверх": "up", "вниз": "down", "влево": "left", "вправо": "right",
    "arrowup": "up", "arrowdown": "down", "arrowleft": "left", "arrowright": "right",
    "prtsc": "printscreen", "prtscr": "printscreen", "print": "printscreen",
    "plus": "=", "плюс": "=", "minus": "-", "минус": "-",
}
_NAMED_KEYS = {
    "ctrl", "alt", "shift", "win", "enter", "esc", "tab", "space", "backspace", "delete", "insert",
    "home", "end", "pageup", "pagedown", "up", "down", "left", "right", "printscreen", "capslock",
    "numlock", "scrolllock", "pause", "apps", "volumeup", "volumedown", "volumemute",
    "playpause", "nexttrack", "prevtrack",
    *(f"f{i}" for i in range(1, 25)),
}
_RU_LAYOUT = dict(zip("йцукенгшщзхъфывапролджэячсмитьбю", "qwertyuiop[]asdfghjkl;'zxcvbnm,."))
_MODIFIER_ORDER = {"ctrl": 0, "alt": 1, "shift": 2, "win": 3}


def parse_hotkey(keys: str) -> list[str]:
    text = keys.strip().lower()
    if not text:
        raise ToolError("не указано сочетание клавиш")
    parts = [p.strip() for p in re.split(r"\s*\+\s*", text)] if "+" in text else text.split()
    result: list[str] = []
    for part in parts:
        if not part:
            continue
        key = _KEY_SYNONYMS.get(part.replace(" ", ""), part.replace(" ", ""))
        if len(key) == 1 and key in _RU_LAYOUT:
            key = _RU_LAYOUT[key]
        if not (key in _NAMED_KEYS or (len(key) == 1 and (key.isascii() and (key.isalnum() or key in "-=[];',./\\`")))):
            raise ToolError(f"неизвестная клавиша «{part}»")
        if key not in result:
            result.append(key)
    if not result or len(result) > 5:
        raise ToolError("сочетание должно содержать от 1 до 5 клавиш")
    return sorted(result, key=lambda k: _MODIFIER_ORDER.get(k, 9))


def format_hotkey(combo: list[str]) -> str:
    return "+".join(k.upper() if len(k) == 1 else k.capitalize() for k in combo)


def is_alt_f4(combo: list[str]) -> bool:
    return "alt" in combo and "f4" in combo


_pyautogui_module = None


def _pyautogui():
    """pyautogui с исправлением: буквы и цифры — по виртуальным кодам клавиш,
    чтобы сочетания работали и при русской раскладке."""
    global _pyautogui_module
    if _pyautogui_module is None:
        import pyautogui

        pyautogui.FAILSAFE = False
        pyautogui.PAUSE = 0.03
        try:
            from pyautogui import _pyautogui_win as backend

            mapping = backend.keyboardMapping
            mapping.update({ch: 0x41 + i for i, ch in enumerate("abcdefghijklmnopqrstuvwxyz")})
            mapping.update({ch: 0x30 + i for i, ch in enumerate("0123456789")})
            mapping.update({"-": 0xBD, "=": 0xBB, "[": 0xDB, "]": 0xDD, ";": 0xBA, "'": 0xDE,
                            ",": 0xBC, ".": 0xBE, "/": 0xBF, "\\": 0xDC, "`": 0xC0})
        except Exception as exc:
            log.debug("Не удалось поправить раскладку pyautogui: %s", exc)
        _pyautogui_module = pyautogui
    return _pyautogui_module


def _confirm_hotkey(ctx: CallContext, keys: str) -> Confirmation | None:
    combo = parse_hotkey(keys)
    if not is_alt_f4(combo):
        return None
    target = ctx.services.focus.target() if ctx.services.focus else winapi.foreground_window()
    if not target:
        raise ToolError("не удалось определить окно, которое закроет Alt+F4")
    title = winapi.window_title(target) or "без названия"
    return Confirmation("Закрыть окно (Alt+F4)",
                        f"Нажать {format_hotkey(combo)} — будет закрыто окно «{title}». "
                        "Несохранённые данные в нём могут быть потеряны.",
                        data=target)


@tool(
    "hotkey",
    "Нажать сочетание клавиш в активном окне, например «ctrl+c», «ctrl+s», «alt+tab», «win+d», "
    "«ctrl+shift+esc». Alt+F4 выполняется только после подтверждения пользователя.",
    {"keys": {"type": "string", "description": "Клавиши через «+», например ctrl+shift+t"}},
    required=("keys",),
    confirm=_confirm_hotkey,
    quick=True,
)
def hotkey(ctx: CallContext, keys: str) -> str:
    combo = parse_hotkey(keys)
    gui = _pyautogui()
    if is_alt_f4(combo):
        target = ctx.prepared
        if not winapi.is_window(target):
            raise ToolError("окно уже закрыто")
        title = winapi.window_title(target)
        # Никогда не шлём Alt+F4 в окно Джарвиса: если переключиться не удалось —
        # закрываем нужное окно напрямую.
        if winapi.activate_window(target) and winapi.foreground_window() == target:
            gui.hotkey(*combo, interval=0.05)
        else:
            winapi.system_close_window(target)
        return f"Окно «{title}» закрыто (Alt+F4)."
    target = ctx.services.focus.focus_target() if ctx.services.focus else winapi.foreground_window()
    gui.hotkey(*combo, interval=0.05)
    title = winapi.window_title(target) if target else ""
    return f"Нажато {format_hotkey(combo)}" + (f" в окне «{title}»." if title else ".")


@tool("screenshot", "Сделать снимок экрана и сохранить его в папку «Изображения\\Jarvis». Возвращает путь к файлу.", quick=True)
def screenshot(ctx: CallContext) -> str:
    from PIL import ImageGrab

    folder = winapi.known_folder("Pictures") / "Jarvis"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"screenshot_{datetime.now():%Y-%m-%d_%H-%M-%S}.png"
    ImageGrab.grab(all_screens=True).save(path)
    return f"Скриншот сохранён: {path}"


def _gb(value: float) -> str:
    return f"{value / 1024 ** 3:.1f}".replace(".", ",")


@tool("system_info", "Состояние компьютера: загрузка процессора, оперативная память, батарея, диск.")
def system_info(ctx: CallContext) -> str:
    cpu = psutil.cpu_percent(interval=0.5)
    memory = psutil.virtual_memory()
    lines = [
        f"Процессор: загрузка {cpu:.0f}%, логических ядер {psutil.cpu_count(logical=True)}.",
        f"Оперативная память: занято {_gb(memory.used)} ГБ из {_gb(memory.total)} ГБ ({memory.percent:.0f}%).",
    ]
    battery = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
    if battery is None:
        lines.append("Батарея: нет (стационарный компьютер).")
    else:
        state = "заряжается" if battery.power_plugged else "работает от батареи"
        text = f"Батарея: {battery.percent:.0f}%, {state}"
        if not battery.power_plugged and battery.secsleft not in (psutil.POWER_TIME_UNLIMITED, psutil.POWER_TIME_UNKNOWN):
            hours, minutes = divmod(int(battery.secsleft) // 60, 60)
            text += f", осталось примерно {hours} ч {minutes} мин"
        lines.append(text + ".")
    try:
        drive = os.environ.get("SystemDrive", "C:") + "\\" if winapi.IS_WINDOWS else "/"
        disk = psutil.disk_usage(drive)
        lines.append(f"Диск {drive}: свободно {_gb(disk.free)} ГБ из {_gb(disk.total)} ГБ.")
    except OSError:
        pass
    uptime = int(time.time() - psutil.boot_time())
    hours, minutes = divmod(uptime // 60, 60)
    lines.append(f"Время работы без перезагрузки: {hours} ч {minutes} мин.")
    return " ".join(lines)


_WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
_MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
           "сентября", "октября", "ноября", "декабря"]


@tool("get_datetime", "Текущие дата, время и день недели.")
def get_datetime(ctx: CallContext) -> str:
    now = datetime.now()
    return (f"Сейчас {now:%H:%M}, {_WEEKDAYS[now.weekday()]}, "
            f"{now.day} {_MONTHS[now.month - 1]} {now.year} года.")


def _minutes_text(minutes: float) -> str:
    if float(minutes).is_integer():
        value = int(minutes)
        return f"{value} {ru_plural(value, ('минуту', 'минуты', 'минут'))}"
    seconds = int(round(minutes * 60))
    if seconds < 60:
        return f"{seconds} {ru_plural(seconds, ('секунду', 'секунды', 'секунд'))}"
    return f"{minutes:g} мин".replace(".", ",")


@tool(
    "timer",
    "Поставить таймер или напоминание: через указанное число минут (minutes) или в указанное время "
    "(at, «ЧЧ:ММ») придёт уведомление Windows и Джарвис скажет об этом.",
    {
        "minutes": {"type": "number", "description": "Через сколько минут сработать (можно дробное: 0.5 — 30 секунд)",
                    "minimum": 0.05, "maximum": 1440},
        "at": {"type": "string", "description": "Время срабатывания «ЧЧ:ММ», например «15:30» (вместо minutes)"},
        "label": {"type": "string", "description": "Короткая подпись, например «чай» или «позвонить маме»"},
    },
    quick=True,
)
def timer(ctx: CallContext, minutes: float | None = None, at: str = "", label: str = "") -> str:
    label = label.strip() or ("Напоминание" if at else "Таймер")
    now = datetime.now()
    if at.strip():
        match = re.fullmatch(r"\s*(\d{1,2})[:.\s](\d{2})\s*", at)
        if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
            raise ToolError(f"время «{at}» не похоже на ЧЧ:ММ")
        moment = now.replace(hour=int(match.group(1)), minute=int(match.group(2)), second=0, microsecond=0)
        if moment <= now:
            moment += timedelta(days=1)
        seconds = (moment - now).total_seconds()
    elif minutes:
        seconds = float(minutes) * 60
    else:
        raise ToolError("укажите minutes (через сколько минут) или at (время «ЧЧ:ММ»)")
    services = ctx.services

    def fire() -> None:
        services.notify("Таймер", f"Сэр, время вышло: {label}." if not at else f"Сэр, напоминаю: {label}.")

    handle = threading.Timer(seconds, fire)
    handle.daemon = True
    handle.start()
    services.timers.append(handle)
    ends = now + timedelta(seconds=seconds)
    if at:
        day = "" if ends.date() == now.date() else " завтра"
        return f"Напоминание «{label}» поставлено на {ends:%H:%M}{day}."
    return f"Таймер «{label}» на {_minutes_text(seconds / 60)} запущен, сработает в {ends:%H:%M:%S}."


@tool(
    "note",
    "Записать заметку в файл «Документы\\Jarvis\\notes.txt» (с датой и временем).",
    {"text": {"type": "string", "description": "Текст заметки"}},
    required=("text",),
    quick=True,
)
def note(ctx: CallContext, text: str) -> str:
    folder = winapi.known_folder("Documents") / "Jarvis"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "notes.txt"
    with open(path, "a", encoding="utf-8") as file:
        file.write(f"[{datetime.now():%Y-%m-%d %H:%M}] {text.strip()}\n")
    return f"Заметка сохранена в {path}."


@tool("lock_pc", "Заблокировать компьютер (экран блокировки Windows).", quick=True)
def lock_pc(ctx: CallContext) -> str:
    winapi.lock_workstation()
    return "Компьютер заблокирован."


def _confirm_sleep(ctx: CallContext) -> Confirmation:
    return Confirmation("Спящий режим", "Перевести компьютер в спящий режим через 3 секунды.")


@tool("sleep_pc", "Перевести компьютер в спящий режим. Требует подтверждения пользователя.", confirm=_confirm_sleep, quick=True)
def sleep_pc(ctx: CallContext) -> str:
    winapi._require_windows()
    handle = threading.Timer(3, winapi.suspend)
    handle.daemon = True
    handle.start()
    return "Компьютер уйдёт в спящий режим через 3 секунды."


def _shutdown_delay(ctx: CallContext) -> int:
    return max(0, int(ctx.config.get("tools.shutdown_delay", 10)))


def _confirm_shutdown(ctx: CallContext) -> Confirmation:
    return Confirmation("Выключение компьютера",
                        f"Выключить компьютер через {_shutdown_delay(ctx)} с. Несохранённые данные в программах будут потеряны.")


def _confirm_restart(ctx: CallContext) -> Confirmation:
    return Confirmation("Перезагрузка компьютера",
                        f"Перезагрузить компьютер через {_shutdown_delay(ctx)} с. Несохранённые данные в программах будут потеряны.")


def _run_shutdown(args: list[str]) -> subprocess.CompletedProcess:
    winapi._require_windows()
    return subprocess.run(["shutdown", *args], capture_output=True, timeout=15, creationflags=winapi.CREATE_NO_WINDOW)


@tool("shutdown_pc", "Выключить компьютер. Требует подтверждения пользователя.", confirm=_confirm_shutdown, quick=True)
def shutdown_pc(ctx: CallContext) -> str:
    delay = _shutdown_delay(ctx)
    result = _run_shutdown(["/s", "/t", str(delay), "/c", "Джарвис: выключение по команде пользователя"])
    if result.returncode != 0:
        raise ToolError(f"команда shutdown завершилась с ошибкой: {winapi.decode_output(result.stderr).strip()}")
    return f"Компьютер выключится через {delay} с. Отменить можно командой «отмени выключение»."


@tool("restart_pc", "Перезагрузить компьютер. Требует подтверждения пользователя.", confirm=_confirm_restart, quick=True)
def restart_pc(ctx: CallContext) -> str:
    delay = _shutdown_delay(ctx)
    result = _run_shutdown(["/r", "/t", str(delay), "/c", "Джарвис: перезагрузка по команде пользователя"])
    if result.returncode != 0:
        raise ToolError(f"команда shutdown завершилась с ошибкой: {winapi.decode_output(result.stderr).strip()}")
    return f"Компьютер перезагрузится через {delay} с. Отменить можно командой «отмени перезагрузку»."


@tool("cancel_shutdown", "Отменить запланированное выключение или перезагрузку компьютера.", quick=True)
def cancel_shutdown(ctx: CallContext) -> str:
    result = _run_shutdown(["/a"])
    if result.returncode != 0:
        return "Запланированного выключения или перезагрузки нет."
    return "Выключение/перезагрузка отменены."


def _confirm_powershell(ctx: CallContext, command: str) -> Confirmation:
    return Confirmation("Команда PowerShell",
                        "Выполнить в PowerShell следующую команду (проверьте её целиком):",
                        details=command)


@tool(
    "run_powershell",
    "Выполнить команду Windows PowerShell и вернуть её вывод. Используй, только если другие "
    "инструменты не подходят. Каждую команду пользователь подтверждает вручную.",
    {"command": {"type": "string", "description": "Команда или короткий скрипт PowerShell"}},
    required=("command",),
    confirm=_confirm_powershell,
    available=lambda config: bool(config.get("tools.allow_powershell", False)),
)
def run_powershell(ctx: CallContext, command: str) -> str:
    timeout = float(ctx.config.get("tools.powershell_timeout", 30))
    try:
        result = winapi.run_powershell(command, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ToolError(f"команда не завершилась за {timeout:.0f} с и была остановлена") from None
    output = (winapi.decode_output(result.stdout) + winapi.decode_output(result.stderr)).strip()
    if len(output) > 3000:
        output = output[:3000] + "\n…(вывод обрезан)"
    return f"Код завершения {result.returncode}.\n{output or '(нет вывода)'}"


# ═══ Код и файлы ═════════════════════════════════════════════════════

_SCRIPT_EXTENSIONS = {".py", ".pyw", ".bat", ".cmd", ".ps1", ".vbs", ".js", ".jse", ".wsf", ".exe", ".msi",
                      ".com", ".scr", ".lnk", ".reg", ".hta", ".jar"}
MAX_FILE_CHARS = 200_000
MAX_READ_CHARS = 12_000


def code_folder(config) -> Path:
    """Папка для кода, который пишет Джарвис (по умолчанию «Документы\\Jarvis\\Code»)."""
    custom = str(config.get("tools.code_folder", "") or "").strip()
    if custom:
        return Path(os.path.expandvars(os.path.expanduser(custom)))
    return winapi.known_folder("Documents") / "Jarvis" / "Code"


def resolve_file(path: str, config) -> Path:
    """Путь к файлу: полный путь, «Рабочий стол\\x.txt» или просто имя — тогда в папке для кода."""
    text = path.strip().strip('"').strip("«»")
    if not text:
        raise ToolError("не указан путь к файлу")
    target = resolve_folder(text)
    if isinstance(target, str):
        raise ToolError(f"«{path}» — системная папка, а не файл")
    if not target.is_absolute():
        target = code_folder(config) / target
    return target


def _outside_home(path: Path) -> bool:
    try:
        path.resolve().relative_to(Path.home().resolve())
        return False
    except ValueError:
        return True


def find_editor(config) -> tuple[str, list[str]] | None:
    """Редактор кода: из настроек, VS Code, Notepad++ или Блокнот. (название, команда)."""
    custom = str(config.get("tools.code_editor", "") or "").strip()
    if custom:
        return os.path.basename(custom), [os.path.expandvars(custom)]
    if not winapi.IS_WINDOWS:
        return None
    local = os.environ.get("LOCALAPPDATA", "")
    program_files = [os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", "")]
    candidates = [
        ("Visual Studio Code", os.path.join(local, "Programs", "Microsoft VS Code", "Code.exe")),
        *[("Visual Studio Code", os.path.join(base, "Microsoft VS Code", "Code.exe")) for base in program_files if base],
        ("Cursor", os.path.join(local, "Programs", "cursor", "Cursor.exe")),
        *[("Notepad++", os.path.join(base, "Notepad++", "notepad++.exe")) for base in program_files if base],
    ]
    for title, exe in candidates:
        if exe and os.path.isfile(exe):
            return title, [exe]
    return "Блокнот", ["notepad.exe"]


def open_in_editor(path: Path, config) -> str:
    editor = find_editor(config)
    if editor is None:
        raise ToolError("редактор кода не найден")
    title, command = editor
    flags = winapi.DETACHED_PROCESS | winapi.CREATE_NEW_PROCESS_GROUP if winapi.IS_WINDOWS else 0
    subprocess.Popen([*command, str(path)], creationflags=flags)
    return title


def _confirm_write_file(ctx: CallContext, path: str, content: str, open: bool = True) -> Confirmation | None:
    target = resolve_file(path, ctx.config)
    reasons = []
    if target.exists():
        reasons.append("файл уже существует и будет перезаписан")
    if _outside_home(target):
        reasons.append("файл находится вне вашей папки пользователя")
    if not reasons:
        return None
    lines = content.count("\n") + 1
    return Confirmation("Записать файл", f"Записать {lines} строк в {target}: {', '.join(reasons)}.",
                        details=content[:4000] + ("\n…" if len(content) > 4000 else ""))


@tool(
    "write_file",
    "Создать или перезаписать текстовый файл: код программы (Python, HTML, JavaScript, C++ и т. д.), "
    "скрипт, заметку, документ. Используй, когда просят написать программу или код: код сохраняется "
    "в файл и открывается в редакторе (VS Code, Notepad++ или Блокнот). Если указано только имя — "
    "файл создаётся в «Документы\\Jarvis\\Code». Перезапись существующего файла — с подтверждением.",
    {
        "path": {"type": "string", "description": "Имя файла с расширением (например snake.py) или полный путь"},
        "content": {"type": "string", "description": "Полное содержимое файла"},
        "open": {"type": "boolean", "description": "Открыть файл в редакторе после записи (по умолчанию да)"},
    },
    required=("path", "content"),
    confirm=_confirm_write_file,
    quick=True,
)
def write_file(ctx: CallContext, path: str, content: str, open: bool = True) -> str:
    if len(content) > MAX_FILE_CHARS:
        raise ToolError(f"слишком большой файл (больше {MAX_FILE_CHARS} символов)")
    target = resolve_file(path, ctx.config)
    target.parent.mkdir(parents=True, exist_ok=True)
    # PowerShell 5 читает UTF-8 без BOM как ANSI — для .ps1 пишем с BOM.
    encoding = "utf-8-sig" if target.suffix.lower() == ".ps1" else "utf-8"
    target.write_text(content, encoding=encoding)
    lines = content.count("\n") + 1
    result = f"Файл сохранён: {target} ({lines} {ru_plural(lines, ('строка', 'строки', 'строк'))})."
    if open:
        try:
            result += f" Открыт в редакторе «{open_in_editor(target, ctx.config)}»."
        except (OSError, ToolError) as exc:
            result += f" Открыть в редакторе не удалось: {exc}."
    return result


def _read_text(path: Path) -> str:
    data = path.read_bytes()
    if b"\0" in data[:4096]:
        raise ToolError(f"{path.name} — двоичный файл, а не текст")
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


@tool(
    "read_file",
    "Прочитать текстовый файл (код, заметки, конфиги), чтобы объяснить, проверить или исправить его. "
    "Если указано только имя — ищет в «Документы\\Jarvis\\Code».",
    {"path": {"type": "string", "description": "Имя файла или полный путь"}},
    required=("path",),
)
def read_file(ctx: CallContext, path: str) -> str:
    target = resolve_file(path, ctx.config)
    if not target.is_file():
        raise ToolError(f"файл не найден: {target}")
    text = _read_text(target)
    more = ""
    if len(text) > MAX_READ_CHARS:
        more = f"\n…(показаны первые {MAX_READ_CHARS} из {len(text)} символов)"
        text = text[:MAX_READ_CHARS]
    return f"Файл {target}:\n{text}{more}"


@tool(
    "open_file",
    "Открыть файл: документ, картинку, видео — в программе по умолчанию; код и скрипты — в редакторе "
    "(не запуская их).",
    {"path": {"type": "string", "description": "Имя файла или полный путь"}},
    required=("path",),
    quick=True,
)
def open_file(ctx: CallContext, path: str) -> str:
    target = resolve_file(path, ctx.config)
    if not target.exists():
        raise ToolError(f"файл не найден: {target}")
    if target.is_dir():
        os.startfile(str(target))  # type: ignore[attr-defined]
        return f"Открыл папку {target}."
    if target.suffix.lower() in _SCRIPT_EXTENSIONS:
        return f"Открыл {target.name} в редакторе «{open_in_editor(target, ctx.config)}»."
    os.startfile(str(target))  # type: ignore[attr-defined]
    return f"Открыл файл {target.name}."


def _python_executable() -> str:
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and (exe.parent / "python.exe").exists():
        return str(exe.parent / "python.exe")  # pythonw не даёт прочитать вывод программы
    return str(exe)


def _code_for_run(ctx: CallContext, code: str, path: str) -> tuple[str, Path | None]:
    if path.strip():
        target = resolve_file(path, ctx.config)
        if not target.is_file():
            raise ToolError(f"файл не найден: {target}")
        return _read_text(target), target
    if not code.strip():
        raise ToolError("укажите code (текст программы) или path (файл .py)")
    return code, None


def _confirm_run_python(ctx: CallContext, code: str = "", path: str = "", wait: bool = True) -> Confirmation:
    text, target = _code_for_run(ctx, code, path)
    where = f"файл {target}" if target else "код ниже"
    mode = "и дождаться результата" if wait else "в отдельном окне"
    return Confirmation("Запуск кода Python", f"Выполнить на компьютере {where} {mode}. Проверьте код целиком:",
                        details=text[:6000] + ("\n…" if len(text) > 6000 else ""), data=(text, target))


@tool(
    "run_python",
    "Запустить программу на Python: код (code) или сохранённый файл (path) — и вернуть её вывод. "
    "Для программ с окном или игр укажи wait=false: они откроются в отдельном окне. Каждый запуск "
    "пользователь подтверждает, видя код целиком.",
    {
        "code": {"type": "string", "description": "Текст программы на Python"},
        "path": {"type": "string", "description": "Файл .py вместо code (имя или полный путь)"},
        "wait": {"type": "boolean", "description": "Ждать завершения и вернуть вывод (по умолчанию да)"},
    },
    confirm=_confirm_run_python,
    available=lambda config: bool(config.get("tools.allow_code", True)),
)
def run_python(ctx: CallContext, code: str = "", path: str = "", wait: bool = True) -> str:
    text, target = ctx.prepared
    folder = target.parent if target else code_folder(ctx.config)
    folder.mkdir(parents=True, exist_ok=True)
    if target is None:
        target = folder / "jarvis_run.py"
        target.write_text(text, encoding="utf-8")
    command = [_python_executable(), "-X", "utf8", str(target)]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    if not wait:
        flags = subprocess.CREATE_NEW_CONSOLE if winapi.IS_WINDOWS else 0  # type: ignore[attr-defined]
        subprocess.Popen(command, cwd=str(folder), env=env, creationflags=flags)
        return f"Программа {target.name} запущена в отдельном окне."
    timeout = float(ctx.config.get("tools.code_timeout", 60))
    try:
        result = subprocess.run(command, cwd=str(folder), env=env, capture_output=True, timeout=timeout,
                                stdin=subprocess.DEVNULL,
                                creationflags=winapi.CREATE_NO_WINDOW if winapi.IS_WINDOWS else 0)
    except subprocess.TimeoutExpired:
        raise ToolError(f"программа работала дольше {timeout:.0f} с и была остановлена "
                        "(для программ с окном используй wait=false)") from None
    output = (winapi.decode_output(result.stdout) + winapi.decode_output(result.stderr)).strip()
    if len(output) > 4000:
        output = output[:2000] + "\n…\n" + output[-1500:]
    return f"Код завершения {result.returncode}.\n{output or '(нет вывода)'}"


_SKIP_DIRS = {"appdata", "node_modules", ".git", "__pycache__", ".venv", "venv", "$recycle.bin", "windows"}


@tool(
    "find_files",
    "Найти файлы и папки по части имени в папках пользователя (Рабочий стол, Документы, Загрузки, "
    "Изображения, Музыка, Видео) или в указанной папке.",
    {
        "name": {"type": "string", "description": "Часть имени файла, например «отчёт» или «.pdf»"},
        "folder": {"type": "string", "description": "Где искать (необязательно): «Загрузки», полный путь"},
    },
    required=("name",),
)
def find_files(ctx: CallContext, name: str, folder: str = "") -> str:
    query = name.strip().strip("*").lower()
    if not query:
        raise ToolError("не указано, что искать")
    if folder.strip():
        roots = [_folder_path(folder)]
    else:
        roots = [winapi.known_folder(k) for k in ("Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos")]
    deadline = time.monotonic() + 8
    found: list[str] = []
    for root in dict.fromkeys(roots):
        if not root.is_dir():
            continue
        for current, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d.lower() not in _SKIP_DIRS and not d.startswith(".")]
            for entry in dirs + files:
                if query in entry.lower():
                    found.append(os.path.join(current, entry))
            if len(found) >= 30 or time.monotonic() > deadline or ctx.cancel.is_set():
                break
        if len(found) >= 30 or time.monotonic() > deadline:
            break
    if not found:
        return f"Ничего не найдено по запросу «{name}»."
    return f"Найдено {len(found)}{'+' if len(found) >= 30 else ''}:\n" + "\n".join(found[:30])


# ═══ Полезное ════════════════════════════════════════════════════════

@tool(
    "clipboard",
    "Буфер обмена: action=get — прочитать текст из буфера (например, чтобы перевести или исправить "
    "скопированное), action=set — положить текст в буфер (пользователь вставит его Ctrl+V).",
    {
        "action": {"type": "string", "enum": ["get", "set"], "description": "get — прочитать, set — записать"},
        "text": {"type": "string", "description": "Текст для action=set"},
    },
    required=("action",),
    quick=lambda args: args.get("action") == "set",
)
def clipboard(ctx: CallContext, action: str, text: str = "") -> str:
    if action == "set":
        winapi.set_clipboard_text(text)
        return f"Скопировал в буфер обмена ({len(text)} символов)."
    content = winapi.get_clipboard_text()
    if not content:
        return "Буфер обмена пуст (или в нём не текст)."
    if len(content) > MAX_READ_CHARS:
        content = content[:MAX_READ_CHARS] + "\n…(обрезано)"
    return f"В буфере обмена:\n{content}"


_CALC_NAMES = {name: getattr(__import__("math"), name) for name in
               ("sqrt", "sin", "cos", "tan", "asin", "acos", "atan", "log", "log10", "log2", "exp", "pi", "e",
                "floor", "ceil", "factorial", "radians", "degrees", "hypot")}
_CALC_NAMES.update({"abs": abs, "round": round, "min": min, "max": max})


def safe_eval(expression: str) -> float | int:
    """Арифметика без exec: числа, + − × ÷, степени, скобки и функции math."""
    import ast
    import operator

    operators = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
                 ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow,
                 ast.USub: operator.neg, ast.UAdd: operator.pos}
    text = expression.strip().replace("^", "**").replace("×", "*").replace("÷", "/").replace("−", "-")
    text = re.sub(r"(\d),(\d)", r"\1.\2", text)
    text = re.sub(r"(\d+(?:\.\d+)?)\s*%", r"(\1/100)", text)
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError:
        raise ToolError(f"не понял выражение «{expression}»") from None

    def walk(node):
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in operators:
            left, right = walk(node.left), walk(node.right)
            if isinstance(node.op, ast.Pow) and (abs(right) > 1000 or abs(left) > 1e100):
                raise ToolError("слишком большая степень")
            return operators[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in operators:
            return operators[type(node.op)](walk(node.operand))
        if isinstance(node, ast.Name) and node.id in _CALC_NAMES and not callable(_CALC_NAMES[node.id]):
            return _CALC_NAMES[node.id]
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _CALC_NAMES
                and not node.keywords):
            args = [walk(a) for a in node.args]
            if node.func.id == "factorial" and (not args or args[0] > 1000):
                raise ToolError("слишком большой факториал")
            return _CALC_NAMES[node.func.id](*args)
        raise ToolError(f"в выражении допустимы только числа, + − × ÷ ^, скобки и функции math: «{expression}»")

    try:
        return walk(tree)
    except ZeroDivisionError:
        raise ToolError("деление на ноль") from None
    except (ValueError, OverflowError, TypeError) as exc:
        raise ToolError(f"не удалось вычислить: {exc}") from None


def format_number(value: float | int) -> str:
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
        value = int(value)
    if isinstance(value, int):
        return f"{value:,}".replace(",", " ")
    return f"{value:.10g}".replace(".", ",")


@tool(
    "calculate",
    "Точно вычислить арифметическое выражение (проценты, степени, корни, тригонометрия). Используй для "
    "любых расчётов вместо подсчёта в уме, например «2500*1.15», «sqrt(2)», «15% * 3000».",
    {"expression": {"type": "string", "description": "Выражение, например (120+80)*1.2 или 2^10"}},
    required=("expression",),
)
def calculate(ctx: CallContext, expression: str) -> str:
    return f"{expression.strip()} = {format_number(safe_eval(expression))}"


def _wind(kmph: Any) -> str:
    try:
        return f"{float(kmph) / 3.6:.0f} м/с"
    except (TypeError, ValueError):
        return "?"


def _signed(value: Any) -> str:
    try:
        number = round(float(value))
    except (TypeError, ValueError):
        return str(value)
    return f"+{number}" if number > 0 else str(number)


def _weather_desc(item: dict) -> str:
    for key in ("lang_ru", "weatherDesc"):
        values = item.get(key) or []
        if values and values[0].get("value"):
            return str(values[0]["value"]).strip().lower()
    return ""


@tool(
    "weather",
    "Погода сейчас и прогноз на сегодня и завтра. Без города — по местоположению компьютера.",
    {"city": {"type": "string", "description": "Город, например «Москва» (необязательно)"}},
)
def weather(ctx: CallContext, city: str = "") -> str:
    import httpx

    place = urllib.parse.quote(city.strip())
    try:
        response = httpx.get(f"https://wttr.in/{place}", params={"format": "j1", "lang": "ru"}, timeout=12,
                             headers={"User-Agent": "Jarvis-assistant"}, follow_redirects=True)
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        raise ToolError(f"не удалось получить погоду (wttr.in): {exc}") from None
    try:
        now = data["current_condition"][0]
        area = data.get("nearest_area") or [{}]
        name = city.strip() or (area[0].get("areaName") or [{}])[0].get("value", "")
        parts = [f"Погода{': ' + name if name else ''}. Сейчас {_signed(now.get('temp_C'))}°C "
                 f"(ощущается как {_signed(now.get('FeelsLikeC'))}°C), {_weather_desc(now)}, "
                 f"ветер {_wind(now.get('windspeedKmph'))}, влажность {now.get('humidity')}%."]
        for title, day in zip(("Сегодня", "Завтра"), (data.get("weather") or [])[:2]):
            hourly = day.get("hourly") or []
            rain = max((int(h.get("chanceofrain") or 0) for h in hourly), default=0)
            middle = hourly[len(hourly) // 2] if hourly else {}
            parts.append(f"{title} от {_signed(day.get('mintempC'))} до {_signed(day.get('maxtempC'))}°C, "
                         f"{_weather_desc(middle) or 'без описания'}, вероятность дождя до {rain}%.")
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ToolError(f"сервис погоды вернул непонятный ответ: {exc}") from None
    return " ".join(parts)


# ═══ Окна ════════════════════════════════════════════════════════════

def _app_windows() -> list[tuple[int, str, str]]:
    """Окна программ: (hwnd, заголовок, процесс), без окна самого Джарвиса."""
    own = os.getpid()
    result = []
    for hwnd in winapi.top_windows():
        title = winapi.window_title(hwnd).strip()
        pid = winapi.window_pid(hwnd)
        if not title or pid == own or winapi.window_class(hwnd) in ("Progman", "Shell_TrayWnd", "WorkerW"):
            continue
        try:
            process = psutil.Process(pid).name()
        except psutil.Error:
            process = "?"
        result.append((hwnd, title, process))
    return result


@tool("list_windows", "Список открытых окон программ (заголовок и процесс).")
def list_windows(ctx: CallContext) -> str:
    winapi._require_windows()
    windows = _app_windows()
    if not windows:
        return "Открытых окон нет."
    return "Открытые окна:\n" + "\n".join(f"- {title} ({process})" for _hwnd, title, process in windows[:40])


def find_window(name: str) -> tuple[int, str] | None:
    query = normalize(clean_app_name(name) or name)
    best: tuple[float, int, str] | None = None
    for hwnd, title, process in _app_windows():
        stem = process.lower().removesuffix(".exe")
        score = max(app_name_score(query, title), app_name_score(query, stem))
        if best is None or score > best[0]:
            best = (score, hwnd, title)
    return (best[1], best[2]) if best and best[0] >= 0.75 else None


_WINDOW_ACTIONS = {"focus": "Переключился на окно", "minimize": "Свернул окно", "maximize": "Развернул окно",
                   "restore": "Восстановил окно"}


@tool(
    "window_action",
    "Действие с окном программы: focus — переключиться на него, minimize — свернуть, maximize — "
    "развернуть на весь экран, restore — восстановить. Окно ищется по названию программы или заголовку.",
    {
        "name": {"type": "string", "description": "Программа или часть заголовка окна, например «хром», «Блокнот»"},
        "action": {"type": "string", "enum": list(_WINDOW_ACTIONS), "description": "Что сделать с окном"},
    },
    required=("name", "action"),
    quick=True,
)
def window_action(ctx: CallContext, name: str, action: str) -> str:
    winapi._require_windows()
    found = find_window(name)
    if found is None:
        raise ToolError(f"не нашёл открытое окно «{name}» (list_windows покажет, какие окна открыты)")
    hwnd, title = found
    if action == "focus":
        if not winapi.activate_window(hwnd, timeout=2):
            raise ToolError(f"Windows не дала переключиться на окно «{title}»")
    else:
        command = {"minimize": winapi.SW_MINIMIZE, "maximize": winapi.SW_MAXIMIZE, "restore": winapi.SW_RESTORE}[action]
        winapi.show_window(hwnd, command)
    return f"{_WINDOW_ACTIONS[action]} «{title}»."
