"""Этапы: действия помощника, ревью, правки зависимого после ваших изменений, автоматическая очередь."""

import time

import pytest
from conftest import wait_for

from idea_graph_core import HUMAN
from idea_graph_engine import stage
from idea_graph_engine.packages import normalize
from idea_graph_engine.runtime import EngineError


def add(project, kind, title, status="accepted", **extra):
    return project.store.apply([{"op": "add_card", "kind": kind, "title": title, "status": status, **extra}],
                               HUMAN)["ids"][0]


def steps(project):
    return {s["id"]: s for s in project.state()["stages"]["st"]["steps"]}


def quiet(note, call, request):
    call("finish", outcome="готово", summary="ок")
    return ""


# ---------------------------------------------------------------------- манифест

def test_manifest_validation_and_public_view():
    base = {"id": "p", "name": "П", "kind": "agent", "skill": "s"}
    for bad in ({"stage": {}}, {"stage": {"steps": []}}, {"stage": {"steps": [{"id": "a"}]}},
                {"stage": {"steps": [{"id": "a", "title": "A"}, {"id": "a", "title": "B"}]}},
                {"stage": {"steps": [{"id": "a", "title": "A", "needs": ["z"]}]}}):
        with pytest.raises(ValueError):
            normalize({**base, **bad})
    from idea_graph_engine.packages import Registry
    registry = Registry(discover=False)
    from conftest import stage_manifest
    registry.register(stage_manifest())
    public = next(p for p in registry.public() if p["id"] == "st")
    assert [s["id"] for s in public["stage"]["steps"]] == ["roles", "scen", "comp"]
    assert public["stage"]["steps"][0]["actions"] == ["suggest", "review", "clarify"]
    assert public["stage"]["hidden_edges"] == ["участвует"] and public["banks"]["reads"] == ["comp"]
    assert public["auto"] == {"targets": "сфера", "limits": [{"key": "n", "name": "Сколько", "default": 3}]}
    assert public["research"] is True and "note" not in public["auto"]


def test_downstream_order():
    from conftest import stage_manifest
    manifest = normalize(stage_manifest())
    assert stage.downstream(manifest, "roles") == ["scen"] and stage.downstream(manifest, "scen") == []


# ---------------------------------------------------------------------- ворота

def test_apply_proposal_validation(make):
    project = make(quiet, stage=True)
    plain = add(project, "роль", "Врач")
    with pytest.raises(EngineError) as info:
        project.apply_proposal(plain)
    assert info.value.code == "not_proposal"
    broken = add(project, "правка", "Правка", attrs={"target": plain, "field": "kind", "new": "x"})
    with pytest.raises(EngineError) as info:
        project.apply_proposal(broken)
    assert info.value.code == "bad_proposal"
    gone = add(project, "правка", "Правка 2", attrs={"target": "c99", "field": "body", "new": "x"})
    with pytest.raises(EngineError) as info:
        project.apply_proposal(gone)
    assert info.value.code == "unknown_card"


# ---------------------------------------------------------------------- помощник шага

def test_suggest_and_clarify_notes_and_errors(make):
    notes = []

    def script(note, call, request):
        notes.append((request.model, note))
        call("finish", outcome="готово", summary="ок")
        return ""

    project = make(script, stage=True)
    role = add(project, "роль", "Врач")
    project.start("st", step="roles", action="suggest")
    wait_for(lambda: project.runs["st"], ("finished",))
    assert notes[0][0] == "main" and "ПРЕДЛОЖИ-РОЛИ" in notes[0][1] and "Шаг этапа «Роли»: Кто участвует?" in notes[0][1]
    assert project.runs["st"].step == "roles" and project.runs["st"].action == "suggest"
    project.start("st", step="roles", action="clarify", target=role)
    wait_for(lambda: project.runs["st"], ("finished",))
    assert "УТОЧНИ Врач" in notes[1][1] and f"Цель: карточка {role}" in notes[1][1]
    with pytest.raises(EngineError) as info:
        project.start("st", step="scen", action="clarify")
    assert info.value.code == "no_action"
    for kwargs in ({"step": "roles", "action": "магия"}, {"action": "suggest"}, {"step": "нет", "action": "suggest"},
                   {"step": "roles", "target": "c99"}):
        with pytest.raises(EngineError):
            project.start("st", **kwargs)
    with pytest.raises(EngineError) as info:
        project.start("clarify", step="roles", action="suggest")
    assert info.value.code == "no_stage"


def test_review_runs_on_reviewer_slot_with_limited_tools(make):
    seen = {}

    def script(note, call, request):
        seen["model"] = request.model
        seen["note"] = note
        seen["tools"] = [t["name"] for t in project.tools_for(request.extra["token"])]
        seen["update"] = call("graph_apply", ops=[{"op": "update_card", "id": "c1", "title": "взлом"}])
        seen["ask"] = call("ask", question="?", options=["а", "б"])
        seen["bank"] = call("bank_read", name="comp")
        call("graph_apply", ops=[{"op": "add_card", "kind": "замечание", "title": "Нет роли «Регистратор»",
                                  "body": "Добавьте роль", "status": "proposed", "parent": "c1"}])
        return ""                                                      # finish не вызвал: ревьюер заканчивает сам

    project = make(script, stage=True)
    project.start("st", step="roles", action="review")
    run = wait_for(lambda: project.runs["st"], ("finished",))
    assert seen["model"] == "rev" and "ПРОВЕРЬ-РОЛИ" in seen["note"] and run.role == "reviewer"
    assert seen["tools"] == ["turn_info", "graph_read", "graph_apply", "bank_read", "finish"]
    assert seen["update"].startswith("ОТКАЗ role_limit") and seen["ask"].startswith("ОТКАЗ unknown_tool")
    assert "записей нет" in seen["bank"]
    remark = next(c for c in project.store.cards.values() if c["kind"] == "замечание")
    assert remark["status"] == "proposed" and project.store.cards["c1"]["title"] == "Запись к врачу без звонков"


def test_new_dialog_after_review_uses_the_main_slot_and_open_question_blocks_assistant(make):
    models = []

    def script(note, call, request):
        models.append(request.model)
        if request.model == "main" and "Начало работы" in note:
            call("ask", question="Кто?", options=["а", "б"])
        else:
            call("finish", outcome="готово", summary="ок")
        return ""

    project = make(script, stage=True)
    project.start("st", step="roles", action="review")
    wait_for(lambda: project.runs["st"], ("finished",))
    project.start("st")
    run = wait_for(lambda: project.runs["st"], ("asking",))
    assert models == ["rev", "main"] and run.role == ""
    with pytest.raises(EngineError) as info:
        project.start("st", step="roles", action="suggest")
    assert info.value.code == "open_question"


# ---------------------------------------------------------------------- автоматическая очередь

def test_auto_queue_runs_every_step_for_every_target(make):
    notes = []

    def script(note, call, request):
        notes.append(note)
        call("finish", outcome="готово", summary="шаг")
        return ""

    project = make(script, stage=True)
    one = add(project, "сфера", "Клиники")
    two = add(project, "сфера", "Аптеки")
    project.settings.data["auto_limits"] = {"st": {"n": 7}}
    project.start("st", auto={"targets": [], "research": False})
    run = wait_for(lambda: project.runs["st"], ("finished", "error"), timeout=15)
    assert run.status == "finished" and run.autorun["pos"] == 6
    joined = "\n".join(notes)
    assert joined.count("АВТО c") == 1 and f"АВТО {one},{two} лимиты={{'n': 7}}" in joined      # вводные режима — один раз
    for marker in ("ПРЕДЛОЖИ-РОЛИ", "ПРЕДЛОЖИ-СЦЕНАРИИ", "ПРЕДЛОЖИ-КОНКУРЕНТОВ"):
        assert joined.count(marker) == 2                                                       # по разу на каждую сферу
    assert f"Цель: карточка {one}" in joined and f"Цель: карточка {two}" in joined
    assert sum(1 for m in project.chat if m["kind"] == "step") == 5
    project.settings.data["auto_unlimited"] = True
    project.start("st", auto={"targets": [one], "research": False})
    wait_for(lambda: project.runs["st"], ("finished",), timeout=15)
    assert "лимиты=None" in "\n".join(notes)


def test_auto_validation(make):
    project = make(quiet, stage=True)
    with pytest.raises(EngineError) as info:
        project.start("st", auto={"targets": []})
    assert info.value.code == "no_target"
    add(project, "сфера", "Клиники")
    with pytest.raises(EngineError) as info:
        project.start("st", auto={"targets": ["c1"]})                    # c1 — «ветка», не сфера
    assert info.value.code == "unknown_target"
    with pytest.raises(EngineError) as info:
        project.start("clarify", auto={"targets": []})
    assert info.value.code == "no_auto"

