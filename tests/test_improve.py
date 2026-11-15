"""Итеративная работа: «что улучшить», открытые вопросы шага, счётчик прогонов, запрет прямых правок чужого, пропуск части."""

import pytest
from conftest import wait_for

from idea_graph_core import HUMAN
from idea_graph_engine.runtime import EngineError


def add(project, kind, title, status="accepted", **extra):
    return project.store.apply([{"op": "add_card", "kind": kind, "title": title, "status": status, **extra}], HUMAN)["ids"][0]


def step_state(project, step_id):
    return next(s for s in project.state()["stages"]["st"]["steps"] if s["id"] == step_id)


def with_checks(project):
    steps = project.registry.get("st")["stage"]["steps"]
    steps[1]["checks"] = lambda ctx, step: [f"У сценария «{c['title']}» не указаны роли" for c in ctx.store.cards.values()
                                             if c["kind"] == "сценарий"]
    steps[0]["help"] = "Выберите роли"


def test_goal_and_existing_result_turn_a_run_into_an_improvement(make):
    notes = []

    def script(note, call, request):
        notes.append(note)
        call("finish", outcome="готово", summary="ок")
        return ""

    project = make(script, stage=True)
    project.start("st", step="roles", action="suggest", goal="добавь роль врача")
    wait_for(lambda: project.runs["st"], ("finished",))
    assert "ЧТО УЛУЧШИТЬ (задача человека): добавь роль врача" in notes[0] and "это УЛУЧШЕНИЕ" not in notes[0]   # результата ещё нет
    add(project, "роль", "Пациент", status="proposed")
    project.start("st", step="roles", action="suggest")
    wait_for(lambda: project.runs["st"], ("finished",))
    assert "это УЛУЧШЕНИЕ" in notes[1] and "не дублируй" in notes[1] and "карточку «правка»" in notes[1]
    assert "Открытые вопросы шага: Не принято карточек: 1" in notes[1] and "ЧТО УЛУЧШИТЬ" not in notes[1]
    project.start("st", step="roles", action="review")                      # ревью не получает вводных про улучшение
    wait_for(lambda: project.runs["st"], ("finished",))
    assert "УЛУЧШЕНИЕ" not in notes[2] and "ПРОВЕРЬ-РОЛИ" in notes[2]


def test_auto_counts_each_step_once(make):
    project = make(lambda note, call, request: call("finish", outcome="готово", summary="ок") and "", stage=True)
    add(project, "сфера", "Клиники")
    project.start("st", auto={"targets": [], "research": False})
    wait_for(lambda: project.runs["st"], ("finished",), timeout=15)
    assert [step_state(project, s)["runs"] for s in ("roles", "scen", "comp")] == [1, 1, 1]


def test_improving_run_cannot_edit_human_or_accepted_cards_but_may_edit_its_own(make):
    results = []

    def script(note, call, request):
        results.append(call("graph_apply", ops=[{"op": "update_card", "id": human, "title": "взлом"}]))
        results.append(call("graph_apply", ops=[{"op": "delete_card", "id": accepted}]))
        results.append(call("graph_apply", ops=[{"op": "update_card", "id": mine, "title": "Моя правка"}]))
        results.append(call("graph_apply", ops=[{"op": "add_card", "kind": "правка", "title": "Правка: Врач", "status": "proposed",
                                                  "attrs": {"target": human, "field": "title", "new": "Терапевт", "reason": "r"}}]))
        call("finish", outcome="готово", summary="ок")
        return ""

    project = make(script, stage=True)
    human = add(project, "роль", "Врач")
    accepted = project.store.apply([{"op": "add_card", "kind": "роль", "title": "Агентская принятая", "status": "accepted"}], "package:st")["ids"][0]
    mine = project.store.apply([{"op": "add_card", "kind": "роль", "title": "Агентская предложенная", "status": "proposed"}], "package:st")["ids"][0]
    project.start("st", step="roles", action="suggest")
    wait_for(lambda: project.runs["st"], ("finished",))
    assert results[0].startswith("ОТКАЗ improve_limit") and results[1].startswith("ОТКАЗ improve_limit")
    assert results[2].startswith("Готово") and project.store.cards[mine]["title"] == "Моя правка"
    assert results[3].startswith("Готово") and project.store.cards[human]["title"] == "Врач"
    # обычный диалог без шага ограничением не связан
    project.runs["st"].step = ""
    project.start("st")
    wait_for(lambda: project.runs["st"], ("finished",))
    assert project.runs["st"].step == ""


def test_reviewer_remarks_are_tagged_with_the_step(make):
    def script(note, call, request):
        call("graph_apply", ops=[{"op": "add_card", "kind": "замечание", "title": "Мало ролей", "status": "proposed", "parent": "c1"}])
        return ""

    project = make(script, stage=True)
    project.start("st", step="roles", action="review")
    wait_for(lambda: project.runs["st"], ("finished",))
    remark = next(c for c in project.store.cards.values() if c["kind"] == "замечание")
    assert remark["attrs"]["step"] == "roles"
    assert [i["text"] for i in step_state(project, "roles")["issues"]] == ["Мало ролей"]

