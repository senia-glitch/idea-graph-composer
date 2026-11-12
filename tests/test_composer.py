"""Карта пакетов: проверка до запуска, выбор стрелки, прогон с петлёй и пошаговый режим."""

import time

import pytest

from idea_graph_composer import Runner, api, choose, maps

PACKAGES = {
    "clarify": {"id": "clarify", "name": "Уточнение", "kind": "agent", "outcomes": ["готово"],
                "reads": ["идея"], "writes": ["решение", "открытый вопрос"]},
    "check": {"id": "check", "name": "Проверка", "kind": "agent", "outcomes": ["чисто", "есть_мешающие"],
              "reads": ["решение"], "writes": ["замечание"]},
    "export": {"id": "export", "name": "Выгрузка", "kind": "code", "outcomes": ["готово"],
               "reads": ["решение"], "writes": []},
    "decider": {"id": "decider", "name": "Решатель", "kind": "decider", "outcomes": [], "reads": [], "writes": []},
}


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("IDEA_GRAPH_HOME", str(tmp_path / "home"))


def test_default_map_and_storage():
    data = maps.default_map(PACKAGES)
    assert [n["package"] for n in data["nodes"]] == ["clarify", "check", "export"]
    assert sorted((e["from"], e["to"], e["when"]) for e in data["edges"]) == [
        ("n1", "n2", "готово"), ("n2", "n1", "есть_мешающие"), ("n2", "n3", "чисто")]
    assert maps.validate(data, PACKAGES) == {"errors": [], "warnings": []}
    maps.save(data)
    assert maps.load(data["id"]) == data and len(maps.load_all()) == 1
    maps.delete(data["id"])
    assert maps.load_all() == []


def test_validation_catches_mistakes():
    data = maps.default_map(PACKAGES)
    data["nodes"].append({"id": "n9", "package": "formalize", "answerer": "", "x": 0, "y": 0})
    data["edges"] += [{"id": "a8", "from": "n2", "to": "n3", "when": "чисто", "max_loops": 3},
                      {"id": "a9", "from": "n1", "to": "n3", "when": "провал", "max_loops": 3}]
    problems = maps.validate(maps.clean(data), PACKAGES)
    text = " | ".join(problems["errors"])
    assert "«formalize» не установлен" in text and "нет итога «провал»" in text and "идут две стрелки" in text
    assert any("нельзя дойти" in w for w in problems["warnings"])
    lonely = maps.clean({"id": "m", "nodes": [{"id": "n1", "package": "export"}], "edges": []})
    assert any("«решение»" in w for w in maps.validate(lonely, PACKAGES)["warnings"])   # никто не создаёт решения
    no_decider = {k: v for k, v in PACKAGES.items() if k != "decider"}
    auto = maps.clean({"id": "m", "nodes": [{"id": "n1", "package": "clarify", "answerer": "decider"}], "edges": []})
    assert any("решатель не установлен" in e for e in maps.validate(auto, no_decider)["errors"])
    with pytest.raises(ValueError):
        maps.clean({"id": "m", "nodes": [{"id": "n1", "package": "x"}], "edges": [{"id": "a", "from": "n1", "to": "zz"}]})


def test_choose():
    data = maps.default_map(PACKAGES)
    assert choose(data, "n2", "чисто", {})[0]["to"] == "n3"
    back = choose(data, "n2", "есть_мешающие", {})[0]
    assert back["to"] == "n1"
    edge, reason = choose(data, "n2", "есть_мешающие", {back["id"]: 3})
    assert edge is None and "Петля исчерпана" in reason
    assert choose(data, "n3", "готово", {}) == (None, "Карта пройдена до конца")
    assert "стрелки нет" in choose(data, "n1", "что-то", {})[1]


class Host:
    """Вместо оболочки: пакеты заканчивают работу сразу с заданными итогами."""

    def __init__(self, tmp_path, outcomes):
        self.dir = tmp_path / ".idea-graph"
        self.folder = tmp_path
        self.outcomes = {key: list(value) for key, value in outcomes.items()}
        self.started = []

    def start(self, package, fresh=False, answerer=""):
        self.started.append((package, answerer))

    def wait(self, package, timeout=None):
        queue = self.outcomes[package]
        outcome = queue.pop(0) if len(queue) > 1 else queue[0]
        if outcome == "!":
            return {"status": "error", "error": "сломался"}
        return {"status": "finished", "outcome": outcome, "summary": "ok"}

    def stop(self, package):
        pass


def finish(runner, statuses=("done", "stopped"), timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        if runner.state["status"] in statuses:
            return runner.public()
        time.sleep(0.01)
    raise AssertionError(runner.state)


def test_run_with_loop_back(tmp_path):
    host = Host(tmp_path, {"clarify": ["готово"], "check": ["есть_мешающие", "чисто"], "export": ["готово"]})
    data = maps.default_map(PACKAGES)
    data["nodes"][0]["answerer"] = "decider"
    runner = Runner(host)
    runner.start(data)
    state = finish(runner)
    assert state["status"] == "done" and state["reason"] == "Карта пройдена до конца"
    assert [p for p, _ in host.started] == ["clarify", "check", "clarify", "check", "export"]
    assert host.started[0] == ("clarify", "decider")                       # кто отвечает — из настроек узла
    assert [h["outcome"] for h in state["history"]] == ["готово", "есть_мешающие", "готово", "чисто", "готово"]
    assert Runner(host).state["status"] == "done"                          # состояние прогона лежит в папке идеи


def test_loop_limit_and_errors(tmp_path):
    host = Host(tmp_path, {"clarify": ["готово"], "check": ["есть_мешающие"], "export": ["готово"]})
    runner = Runner(host)
    runner.start(maps.default_map(PACKAGES))
    state = finish(runner)
    assert state["status"] == "stopped" and "Петля исчерпана" in state["reason"]
    assert [p for p, _ in host.started].count("check") == 3            # каждая стрелка — не больше трёх раз
    broken = Host(tmp_path, {"clarify": ["!"]})
    runner = Runner(broken)
    runner.start(maps.default_map(PACKAGES))
    assert "сломался" in finish(runner)["reason"]


def test_step_mode(tmp_path):
    host = Host(tmp_path, {"clarify": ["готово"], "check": ["чисто"], "export": ["готово"]})
    runner = Runner(host)
    runner.start(maps.default_map(PACKAGES), mode="step")
    assert finish(runner, ("waiting_next",))["current"] == "n2" and len(host.started) == 1
    runner.next()
    end = time.time() + 5
    while runner.state["current"] != "n3" and time.time() < end:
        time.sleep(0.01)
    assert finish(runner, ("waiting_next",))["current"] == "n3" and len(host.started) == 2
    runner.stop()
    assert finish(runner)["reason"] == "Остановлено человеком" and len(host.started) == 2


def test_api_creates_default_map():
    class Registry:
        items = PACKAGES

        def public(self):
            return list(PACKAGES.values())

    class App:
        registry = Registry()
        project = None

    data = api(App(), "GET", "maps", {})
    assert len(data["maps"]) == 1 and data["has_decider"] is True
    assert [p["id"] for p in data["packages"]] == ["clarify", "check", "export"]
    assert api(App(), "GET", "state", {}) == {"state": None, "runs": {}}
    saved = api(App(), "POST", "save", {"map": {**data["maps"][0], "name": "Моя карта"}})
    assert saved["map"]["name"] == "Моя карта" and saved["errors"] == []
