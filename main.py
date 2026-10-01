"""Запуск голосового ассистента «Джарвис»: python main.py

python main.py --check — диагностика микрофона, голоса, модели и инструментов в консоли.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from jarvis.config import ROOT_DIR, load_config


def setup_logging(console_level: int = logging.INFO) -> None:
    log_dir = ROOT_DIR / "logs"
    log_dir.mkdir(exist_ok=True)
    handlers: list[logging.Handler] = [
        RotatingFileHandler(log_dir / "jarvis.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"),
    ]
    if sys.stderr is not None:  # при запуске через pythonw консоли нет
        console = logging.StreamHandler()
        console.setLevel(console_level)
        handlers.append(console)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s",
                        handlers=handlers)
    for noisy in ("httpx", "httpcore", "urllib3", "PIL", "comtypes", "google_genai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main() -> None:
    check = "--check" in sys.argv[1:]
    setup_logging(logging.WARNING if check else logging.INFO)  # отчёт диагностики не смешиваем с логом
    log = logging.getLogger("jarvis")
    if sys.version_info < (3, 10):
        sys.exit("Нужен Python 3.10 или новее.")
    if sys.platform != "win32":
        log.warning("Джарвис рассчитан на Windows 10/11: управление ПК и горячая клавиша на этой ОС не работают.")
    config = load_config()
    if check:
        from jarvis.diagnostics import run_cli

        sys.exit(run_cli(config))
    from jarvis.app import JarvisApp

    log.info("Запуск Джарвиса (бэкенд: %s)", config.get("llm.backend"))
    JarvisApp(config).run()


if __name__ == "__main__":
    main()
