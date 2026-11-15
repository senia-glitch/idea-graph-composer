"""Общее для тестов движка: «агент» без модели, который ходит по сценарию."""

import time

import pytest

from idea_graph_core import Store
from idea_graph_engine.agents import Adapter, Adapters, TurnResult
from idea_graph_engine.config import Settings
from idea_graph_engine.packages import Registry
from idea_graph_engine.runtime import EngineError, GraphError, Project


class ScriptAdapter(Adapter):
    """Вместо запуска командной строки вызывает функцию-сценарий ``script(note, call, request)``.

    Один и тот же «агент» обслуживает основного агента, советников, исследователей и ревьюера: кто это,
    сценарий узнаёт по ``request.model`` (в тестах слотам даны модели ``a1``…, ``r1``…, ``rev``) и по вводным.
    """

    name = "script"
    label = "Сценарий"

    def __init__(self, script):
        self.script = script
        self.requests = []

    def found(self):
        return "script"

    def run(self, request):
        project, token = request.extra["project"], request.extra["token"]
        self.requests.append(request)

        def call(tool, **args):
            try:
                return project.tool_call(token, tool, args)
            except (EngineError, GraphError) as error:
                return "ОТКАЗ " + error.code + ": " + error.message

        text = self.script(request.note, call, request) or ""
        return TurnResult(session_id=request.session_id or "s1", text=text, cost=0.01)


def wait_for(run_getter, statuses, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        run = run_getter()
        if run is not None and run.status in statuses:
            return run
        time.sleep(0.01)
    raise AssertionError(f"статус не стал {statuses}: {getattr(run_getter(), 'status', None)}")


def stage_manifest(**over):
    """Пример этапа: два шага («роли», «сценарии»), банк, автоматический режим и исследование."""
    manifest = {
        "id": "st", "name": "Этап", "kind": "agent", "skill": "Строй.", "outcomes": ["готово", "есть_пробелы"],
        "banks": {"reads": ["comp"], "writes": ["comp"]},
        "stage": {
            "roots": "сфера", "hidden_edges": ["участвует"], "kinds": [{"kind": "роль", "title": "Роль"}],
            "steps": [
                {"id": "roles", "title": "Роли", "question": "Кто участвует?", "produces": ["роль"], "needs": [],
                 "prompts": {"suggest": "ПРЕДЛОЖИ-РОЛИ", "review": "ПРОВЕРЬ-РОЛИ",
                             "clarify": lambda ctx, step, target: "УТОЧНИ " + (target["title"] if target else "-")}},
                {"id": "scen", "title": "Сценарии", "question": "Что делают?", "produces": ["сценарий"],
                 "needs": ["roles"], "prompts": {"suggest": "ПРЕДЛОЖИ-СЦЕНАРИИ"}},
                {"id": "comp", "title": "Конкуренты", "question": "Кто конкурент?", "produces": [], "needs": [],
                 "produces_bank": "comp", "research": True,
                 "prompts": {"suggest": "ПРЕДЛОЖИ-КОНКУРЕНТОВ"}},
            ],
            "auto": {"targets": "сфера", "limits": [{"key": "n", "name": "Сколько", "default": 3}],
                     "note": lambda ctx, targets, limits: "АВТО " + ",".join(c["id"] for c in targets) + f" лимиты={limits}"},
            "research": {
                "skill": "Исследуй.",
                "tasks": lambda ctx, step, target, materials: [
                    {"title": "задача-1", "note": f"ИССЛ задача-1 материалов={len(materials)}"},
                    {"title": "задача-2", "note": "ИССЛ задача-2"}],
                "merge": lambda ctx, step, target, results: "СВОДКА " + "; ".join(
                    f"{r['task']['title']}:{len(r['reports'])}" for r in results),
            },
        },
    }
    manifest.update(over)
    return manifest


@pytest.fixture
def make(tmp_path):
    def build(script, manifests=(), stage=False):
        store = Store.create(tmp_path / "idea", "Запись к врачу")
        store.apply([{"op": "add_card", "kind": "ветка", "title": "Запись к врачу без звонков",
                      "body": "Пациент выбирает время сам"}], "human")
        settings = Settings(tmp_path / "settings.json")
        settings.data["default_slot"] = {"agent": "script", "model": "main", "effort": ""}
        settings.data["researchers"] = [{"agent": "script", "model": f"r{i}", "effort": ""} for i in (1, 2, 3)]
        settings.data["advisers"] = [{"agent": "script", "model": f"a{i}", "effort": ""} for i in (1, 2, 3)]
        settings.data["reviewer"] = {"agent": "script", "model": "rev", "effort": ""}
        registry = Registry(discover=False)
        registry.register({"id": "clarify", "name": "Уточнение", "kind": "agent", "skill": "Уточняй идею.",
                           "outcomes": ["готово"]})
        for manifest in ([stage_manifest()] if stage else []) + list(manifests):
            registry.register(manifest)
        adapters = Adapters()
        adapter = ScriptAdapter(script)
        adapters.add(adapter)
        project = Project(tmp_path / "idea", settings, registry, adapters)
        project.PROPAGATE_DELAY = 0.05
        project.test_adapter = adapter
        return project

    return build
