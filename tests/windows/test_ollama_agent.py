"""Настоящая языковая модель управляет Windows: «Открой блокнот» → open_app → Блокнот открыт.

Нужны запущенная Ollama и скачанная модель (имя — в переменной JARVIS_OLLAMA_MODEL).
"""

import os
import sys

import pytest

if sys.platform != "win32":
    pytest.skip("проверки только для Windows", allow_module_level=True)

import threading  # noqa: E402

from jarvis.agent import Agent  # noqa: E402
from jarvis.llm import create_backend  # noqa: E402

from .conftest import ENABLED, kill_all, processes, wait_for  # noqa: E402

MODEL = os.environ.get("JARVIS_OLLAMA_MODEL", "")
pytestmark = pytest.mark.skipif(not (ENABLED and MODEL), reason="нужны JARVIS_WINDOWS_E2E=1 и JARVIS_OLLAMA_MODEL")


class Events:
    def __init__(self):
        self.texts, self.calls, self.results, self.errors = [], [], [], []

    def on_state(self, state):
        pass

    def on_text(self, delta):
        self.texts.append(delta)

    def on_turn_end(self):
        pass

    def on_tool_start(self, call):
        self.calls.append(call)
        print(f"  → {call.name}({call.arguments})")

    def on_tool_result(self, call, result):
        self.results.append(result)
        print(f"  ← {result.status}: {result.text[:200]}")

    def on_error(self, message, hint=""):
        self.errors.append(f"{message} {hint}")


@pytest.fixture
def agent(config, registry):
    config.set("ollama.model", MODEL)
    backend = create_backend("ollama", config)
    backend.check()
    backend.warmup(str(config.get("system_prompt")), registry.schemas())
    events = Events()
    agent = Agent(config, registry, lambda: backend, events)
    agent.events_log = events
    yield agent
    backend.close()


def ask(agent, text: str):
    events = agent.events_log
    start = len(events.calls)
    print(f"\nПользователь: {text}")
    outcome = agent.run(text, threading.Event())
    print(f"Джарвис: {''.join(events.texts).strip()}")
    events.texts.clear()
    assert not events.errors, events.errors
    return outcome, events.calls[start:]


def test_model_controls_the_pc(agent, confirmer):
    kill_all("notepad.exe")
    try:
        _outcome, calls = ask(agent, "Открой блокнот")
        assert any(c.name == "open_app" for c in calls), [c.name for c in calls]
        assert wait_for(lambda: processes("notepad.exe"), 15), "Блокнот не запустился"

        _outcome, calls = ask(agent, "Который час?")
        assert any(c.name == "get_datetime" for c in calls), [c.name for c in calls]

        _outcome, calls = ask(agent, "Закрой блокнот")
        assert any(c.name == "close_app" for c in calls), [c.name for c in calls]
        assert confirmer.asked and "блокнот" in (confirmer.asked[-1].title + confirmer.asked[-1].text).lower()
        assert wait_for(lambda: not processes("notepad.exe"), 15), "Блокнот не закрылся"
    finally:
        kill_all("notepad.exe")
