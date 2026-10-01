import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.config import DEFAULTS, Config, deep_merge  # noqa: E402


@pytest.fixture
def config(tmp_path):
    """Настройки по умолчанию + алиасы из настоящего config.yaml (без записи на диск);
    память, сценарии и прочие данные — во временной папке."""
    import yaml

    raw = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    config = Config(deep_merge(DEFAULTS, raw), path=None)
    config.set("paths.data", str(tmp_path / "jarvis-data"))
    return config
