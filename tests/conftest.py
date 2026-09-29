import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.config import DEFAULTS, Config, deep_merge  # noqa: E402


@pytest.fixture
def config():
    """Настройки по умолчанию + алиасы из настоящего config.yaml (без записи на диск)."""
    import yaml

    raw = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    return Config(deep_merge(DEFAULTS, raw), path=None)
