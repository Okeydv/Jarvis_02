import threading

from jarvis.agent import Agent, trim_history
from jarvis.llm.base import AssistantTurn, LLMBackend, LLMError, ToolCall
from jarvis.tools import ToolRegistry, ToolServices


class ScriptedBackend(LLMBackend):
    """Отвечает заранее заданными ходами и запоминает, что ему присылали."""

    name = "fake"

    def __init__(self, config, turns):
        super().__init__(config)
        self.turns = list(turns)
        self.calls = []

    @property
    def model(self):
        return "fake"

    def stream_chat(self, system, messages, tools, allow_tools, cancel, on_text):
        self.calls.append({"system": system, "messages": [dict(m) for m in messages],
                           "tools": [t["name"] for t in tools], "allow_tools": allow_tools})
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        if callable(turn):
            turn = turn(cancel)
        for word in turn.text.split(" "):
            on_text(word + " ")
        return turn


class Events:
    def __init__(self):
        self.text = ""
        self.states = []
        self.tools = []
        self.errors = []

    def on_state(self, state):
        self.states.append(state)

    def on_text(self, delta):
        self.text += delta

    def on_turn_end(self):
        pass

    def on_tool_start(self, call):
        pass

    def on_tool_result(self, call, result):
        self.tools.append((call.name, result.status, result.text))

    def on_error(self, message, hint=""):
        self.errors.append((message, hint))


def make_agent(config, turns):
    backend = ScriptedBackend(config, turns)
    events = Events()
    agent = Agent(config, ToolRegistry(ToolServices(config)), lambda: backend, events)
    return agent, backend, events


def test_tool_loop_and_final_answer(config):
    agent, backend, events = make_agent(config, [
        AssistantTurn(tool_calls=[ToolCall(name="get_datetime", arguments={})]),
        AssistantTurn(text="Сейчас полдень, сэр."),
    ])
    outcome = agent.run("Который час?", threading.Event())
    assert outcome.text == "Сейчас полдень, сэр."
    assert outcome.tools_used == 1 and outcome.tool_failures == 0
    assert events.tools[0][0] == "get_datetime" and events.tools[0][1] == "ok"
    assert events.states == ["thinking", "executing", "thinking"]
    # второй запрос к модели содержит результат инструмента
    roles = [m["role"] for m in backend.calls[1]["messages"]]
    assert roles == ["user", "assistant", "tool"]
    assert "Ты — Джарвис" in backend.calls[0]["system"]
    assert "open_app" in backend.calls[0]["tools"]


def test_step_limit_forces_text_on_last_step(config):
    looping = [AssistantTurn(tool_calls=[ToolCall(name="get_datetime", arguments={})]) for _ in range(4)]
    agent, backend, events = make_agent(config, looping + [AssistantTurn(text="Хватит циклов, сэр.")])
    outcome = agent.run("Зацикливайся", threading.Event())
    assert len(backend.calls) == 5  # не больше 5 шагов
    assert [c["allow_tools"] for c in backend.calls] == [True, True, True, True, False]
    assert outcome.text == "Хватит циклов, сэр."


def test_tool_error_goes_back_to_model(config):
    agent, backend, events = make_agent(config, [
        AssistantTurn(tool_calls=[ToolCall(name="set_volume", arguments={"level": 500})]),
        AssistantTurn(text="Не получилось, сэр."),
    ])
    outcome = agent.run("Громкость 500", threading.Event())
    assert outcome.tool_failures == 1
    tool_message = backend.calls[1]["messages"][-1]
    assert tool_message["role"] == "tool" and tool_message["content"].startswith("Ошибка")


def test_unknown_tool_is_reported_not_executed(config):
    agent, backend, events = make_agent(config, [
        AssistantTurn(tool_calls=[ToolCall(name="rm_rf", arguments={})]),
        AssistantTurn(text="Такого не умею."),
    ])
    agent.run("Удали всё", threading.Event())
    assert events.tools[0][1] == "error" and "не существует" in events.tools[0][2]


def test_llm_error_rolls_back_user_message(config):
    agent, backend, events = make_agent(config, [LLMError("Ollama не запущена", "ollama serve")])
    outcome = agent.run("Привет", threading.Event())
    assert outcome.failed
    assert events.errors == [("Ollama не запущена", "ollama serve")]
    assert agent.history == []


def test_cancel_stops_loop(config):
    cancel = threading.Event()

    def cancelled_turn(_cancel):
        cancel.set()
        return AssistantTurn(text="Начинаю длинный", tool_calls=[ToolCall(name="get_datetime", arguments={})])

    agent, backend, events = make_agent(config, [cancelled_turn])
    outcome = agent.run("Расскажи", cancel)
    assert outcome.cancelled
    assert events.tools == []  # инструменты после «Стоп» не выполняются
    assert agent.history[-1]["role"] == "assistant" and "tool_calls" not in agent.history[-1]


def test_history_window_limit(config):
    config.set("llm.history_messages", 6)
    turns = [AssistantTurn(text=f"Ответ {i}.") for i in range(6)]
    agent, backend, events = make_agent(config, turns)
    for i in range(6):
        agent.run(f"Вопрос {i}", threading.Event())
    last = backend.calls[-1]["messages"]
    assert len(last) <= 6
    assert last[0]["role"] == "user"
    assert last[-1]["content"] == "Вопрос 5"


def test_trim_history_keeps_current_request():
    history = [{"role": "user", "content": "старое"}, {"role": "assistant", "content": "ок"},
               {"role": "user", "content": "новое"}]
    history += [{"role": "assistant", "content": "", "tool_calls": []}, {"role": "tool", "content": "r"}] * 15
    window = trim_history(history, 20)
    assert window[0]["content"] == "новое"
    assert trim_history([], 20) == []


def test_simple_action_is_answered_without_second_model_call(config, monkeypatch, tmp_path):
    from jarvis import winapi

    monkeypatch.setattr(winapi, "known_folder", lambda name: tmp_path / name)
    agent, backend, events = make_agent(config, [
        AssistantTurn(tool_calls=[ToolCall(name="note", arguments={"text": "купить молоко"})]),
    ])
    outcome = agent.run("Запиши заметку купить молоко", threading.Event())
    assert len(backend.calls) == 1 and outcome.fast  # модель вызвана один раз
    assert outcome.text.startswith("Готово, сэр. Заметка сохранена")
    assert events.text.strip() == outcome.text  # ответ показан и озвучен
    assert agent.history[-1] == {"role": "assistant", "content": outcome.text}


def test_multi_step_request_goes_back_to_model(config, monkeypatch, tmp_path):
    from jarvis import winapi

    monkeypatch.setattr(winapi, "known_folder", lambda name: tmp_path / name)
    agent, backend, events = make_agent(config, [
        AssistantTurn(tool_calls=[ToolCall(name="note", arguments={"text": "купить молоко"})]),
        AssistantTurn(text="Записал и проверил, сэр."),
    ])
    outcome = agent.run("Запиши заметку и скажи, что записал", threading.Event())
    assert len(backend.calls) == 2 and not outcome.fast


def test_fast_replies_can_be_disabled(config, monkeypatch, tmp_path):
    from jarvis import winapi

    monkeypatch.setattr(winapi, "known_folder", lambda name: tmp_path / name)
    config.set("llm.fast_replies", False)
    agent, backend, events = make_agent(config, [
        AssistantTurn(tool_calls=[ToolCall(name="note", arguments={"text": "x"})]),
        AssistantTurn(text="Готово."),
    ])
    agent.run("Запиши заметку икс", threading.Event())
    assert len(backend.calls) == 2


def test_cancelled_action_gets_short_reply(config):
    agent, backend, events = make_agent(config, [
        AssistantTurn(tool_calls=[ToolCall(name="shutdown_pc", arguments={})]),
    ])
    outcome = agent.run("Выключи компьютер", threading.Event())  # подтверждать некому — отмена
    assert outcome.fast and outcome.text == "Как скажете, сэр, отменяю."
    assert events.tools[0][1] == "cancelled"


def test_questions_still_go_to_model(config):
    agent, backend, events = make_agent(config, [
        AssistantTurn(tool_calls=[ToolCall(name="get_datetime", arguments={})]),
        AssistantTurn(text="Сейчас утро, сэр."),
    ])
    outcome = agent.run("Сколько дней до нового года", threading.Event())
    assert len(backend.calls) == 2 and outcome.text == "Сейчас утро, сэр."


def test_single_action_detection_and_speakable_reply():
    from jarvis.agent import is_single_action, quick_reply
    from jarvis.tools import ToolResult

    assert is_single_action("Открой блокнот")
    assert is_single_action("Сделай погромче")
    assert not is_single_action("Открой блокнот и напиши привет")
    assert not is_single_action("Открой ютуб, потом включи музыку")
    assert not is_single_action("Сделай скриншот, а потом открой его")
    reply = quick_reply([ToolResult("ok", "Запущено «блокнот» (notepad.exe).")], said_something=False)
    assert reply == "Готово, сэр. Запущено «блокнот»."
    assert quick_reply([ToolResult("ok", "Громкость установлена на 30%.")], said_something=True) == \
        "Громкость установлена на 30%."


def test_old_long_tool_results_are_compacted(config):
    from jarvis.agent import compact_history

    code = "print('x')\n" * 200
    call = ToolCall(name="write_file", arguments={"path": "a.py", "content": code}, id="c1")
    history = [
        {"role": "user", "content": "Напиши программу"},
        {"role": "assistant", "content": "", "tool_calls": [call]},
        {"role": "tool", "tool_call_id": "c1", "name": "write_file", "content": "Файл сохранён. " + "x" * 2000},
        {"role": "assistant", "content": "Готово."},
        {"role": "user", "content": "Прочитай файл"},
        {"role": "tool", "tool_call_id": "c2", "name": "read_file", "content": "y" * 3000},
    ]
    window = compact_history(history)
    assert len(window[1]["tool_calls"][0].arguments["content"]) < 400
    assert window[1]["tool_calls"][0].id == "c1" and call.arguments["content"] == code  # оригинал не тронут
    assert "сокращено" in window[2]["content"] and len(window[2]["content"]) < 600
    assert window[5]["content"] == "y" * 3000  # текущий запрос — целиком
