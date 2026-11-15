"""Работа без ворот: устаревание после правок принятого, проверка целого этапа, открытые вопросы, документ."""

import time

import pytest
from conftest import wait_for

from idea_graph_core import HUMAN
from idea_graph_engine.runtime import EngineError


def add(project, kind, title, status="accepted", **extra):
    return project.store.apply([{"op": "add_card", "kind": kind, "title": title, "status": status, **extra}], HUMAN)["ids"][0]


def step_state(project, step_id):
    return next(s for s in project.state()["stages"]["st"]["steps"] if s["id"] == step_id)


def quiet(note, call, request):
    call("finish", outcome="готово", summary="ок")
    return ""


# ---------------------------------------------------------------------- устаревание и правки зависимого

def test_editing_an_accepted_card_marks_started_dependents_and_agent_proposes_edits(make):
    seen = {"notes": [], "denied": []}

    def script(note, call, request):
        seen["notes"].append(note)
        seen["denied"].append(call("graph_apply", ops=[{"op": "update_card", "id": "c2", "title": "взлом"}]))
        seen["denied"].append(call("ask", question="?", options=["а", "б"]))
        call("graph_apply", ops=[{"op": "add_card", "kind": "правка", "title": "Правка: Сценарий записи", "body": "Поменять роль",
                                  "status": "proposed", "attrs": {"target": scenario, "field": "title", "new": "Сценарий записи у терапевта",
                                                                  "reason": "роль переименована"}}])
        call("finish", outcome="готово", summary="предложил правку")
        return ""

    project = make(script, stage=True)
    role = add(project, "роль", "Врач")
    scenario = add(project, "сценарий", "Сценарий записи")
    project.human_ops([{"op": "update_card", "id": role, "title": "Терапевт"}])
    state = step_state(project, "scen")
    assert state["stale"]["cards"] == [role] and "Врач" in state["stale"]["reason"] and step_state(project, "roles")["stale"] is None
    run = wait_for(lambda: project.runs.get("st"), ("finished", "error"))
    assert run.status == "finished" and run.role == "proposer"
    assert "Человек изменил утверждённые карточки" in seen["notes"][0] and "Сценарии" in seen["notes"][0]
    assert seen["denied"][0].startswith("ОТКАЗ role_limit") and seen["denied"][1].startswith("ОТКАЗ unknown_tool")
    proposal = next(c for c in project.store.cards.values() if c["kind"] == "правка")
    project.apply_proposal(proposal["id"])
    assert project.store.cards[scenario]["title"] == "Сценарий записи у терапевта" and proposal["id"] not in project.store.cards
    project.clear_stale("st", "scen")
    assert step_state(project, "scen")["stale"] is None


def test_only_edits_of_accepted_cards_with_started_dependents_matter(make):
    project = make(quiet, stage=True)
    project.settings.data["auto_propagate"] = False
    proposed = add(project, "роль", "Врач", status="proposed")
    accepted = add(project, "роль", "Пациент")
    project.human_ops([{"op": "update_card", "id": proposed, "title": "Терапевт"}])      # ещё не принята: не в счёт
    project.human_ops([{"op": "update_card", "id": accepted, "title": "Клиент"}])        # сценариев ещё нет
    assert step_state(project, "scen")["stale"] is None
    add(project, "сценарий", "Приём")
    project.human_ops([{"op": "update_card", "id": accepted, "status": "accepted"}])     # только статус
    assert step_state(project, "scen")["stale"] is None
    project.human_ops([{"op": "update_card", "id": accepted, "body": "уточнил"}])
    assert step_state(project, "scen")["stale"] is not None
    project.clear_stale("st", "scen")
    project.human_ops([{"op": "delete_card", "id": accepted}])
    assert "удалена" in step_state(project, "scen")["stale"]["reason"]


def test_auto_propagate_can_be_switched_off_and_skips_busy_package(make):
    project = make(quiet, stage=True)
    project.settings.data["auto_propagate"] = False
    role = add(project, "роль", "Врач")
    add(project, "сценарий", "Приём")
    project.human_ops([{"op": "update_card", "id": role, "title": "Терапевт"}])
    time.sleep(0.3)
    assert "st" not in project.runs and step_state(project, "scen")["stale"] is not None
    project.settings.data["auto_propagate"] = True
    project.clear_stale("st", "scen")
    project.start("st", fresh=True)
    wait_for(lambda: project.runs["st"], ("finished",))
    project.runs["st"].question = {"id": "q", "question": "?", "options": [{"title": "а"}, {"title": "б"}]}
    project.runs["st"].status = "asking"
    project.human_ops([{"op": "update_card", "id": role, "title": "Лор"}])
    time.sleep(0.3)
    assert project.runs["st"].role == ""                                  # идёт диалог: правок не запускали


# ---------------------------------------------------------------------- проверка и уточнение целого этапа

def test_phase_review_and_clarify_join_the_prompts_of_all_its_steps(make):
    notes = []

    def script(note, call, request):
        notes.append((request.model, note))
        call("graph_apply", ops=[{"op": "add_card", "kind": "замечание", "title": "Мало ролей", "status": "proposed", "parent": "c1"}]) \
            if request.model == "rev" else call("finish", outcome="готово", summary="ок")
        return ""

    project = make(script, stage=True)
    project.start("st", phase="Процессы", action="review")
    run = wait_for(lambda: project.runs["st"], ("finished",))
    assert notes[0][0] == "rev" and run.role == "reviewer" and run.step == "roles"
    assert "Крупный этап «Процессы»" in notes[0][1] and "ПРОВЕРЬ-РОЛИ" in notes[0][1] and "ПРОВЕРЬ-СЦЕНАРИИ" in notes[0][1]
    remark = next(c for c in project.store.cards.values() if c["kind"] == "замечание")
    assert remark["attrs"]["step"] == "roles"
    project.start("st", phase="Процессы", action="clarify")
    wait_for(lambda: project.runs["st"], ("finished",))
    assert notes[1][0] == "main" and "УТОЧНИ -" in notes[1][1]
    for kwargs in ({"phase": "Разведка", "action": "review"}, {"phase": "Нет такого", "action": "clarify"}):
        with pytest.raises(EngineError) as info:
            project.start("st", **kwargs)
        assert info.value.code == "no_action"


def test_public_steps_carry_phase_and_label(make):
    project = make(quiet, stage=True)
    from idea_graph_engine.packages import Registry
    registry = Registry(discover=False)
    registry.register(project.registry.get("st"))
    steps = registry.public()[0]["stage"]["steps"]
    assert [(s["phase"], s["label"]) for s in steps] == [("Процессы", "Предложить роли"), ("Процессы", "Предложить сценарии"), ("Разведка", "Найти конкурентов")]
    assert not hasattr(project, "approve_step") and not hasattr(project, "skip_stage")


# ---------------------------------------------------------------------- открытые вопросы и документ

def test_open_issues_collect_pending_remarks_proposals_stale_and_checks(make):
    project = make(lambda note, call, request: "", stage=True)
    project.registry.get("st")["stage"]["steps"][1]["checks"] = lambda ctx, step: [
        f"У сценария «{c['title']}» не указаны роли" for c in ctx.store.cards.values() if c["kind"] == "сценарий"]
    role = add(project, "роль", "Врач")
    scenario = add(project, "сценарий", "Приём", status="proposed")
    project.human_ops([{"op": "update_card", "id": role, "title": "Терапевт"}])
    add(project, "замечание", "Нет роли «Регистратор»", status="proposed", parent=role, attrs={"step": "roles"})
    add(project, "замечание", "Про сценарий", status="proposed", parent=scenario)
    add(project, "правка", "Правка: Приём", status="proposed", attrs={"target": scenario, "field": "title", "new": "Приём у терапевта"})
    project.store.bank("comp").put("competitor", {"name": "Докдок", "state": "proposed"}, "package:st")
    issues = lambda sid: [(i["type"], i["text"]) for i in step_state(project, sid)["issues"]]    # noqa: E731
    assert issues("roles") == [("remark", "Нет роли «Регистратор»")]
    assert [t for t, _ in issues("scen")] == ["pending", "stale", "remark", "proposal", "check"]
    assert issues("comp") == [("pending", "Не принято записей банка: 1")]
    assert "status" not in step_state(project, "roles")                    # статусов шагов нет


def test_document_is_assembled_from_packages(make):
    project = make(quiet, stage=True)
    assert "Документ пока пуст" not in project.document()                  # раздел пакета пишет «## Роли» даже без карточек
    add(project, "роль", "Пациент")
    add(project, "роль", "Врач")
    text = project.document()
    assert text.startswith("# Запись к врачу\n") and "## Роли\n- Пациент\n- Врач" in text
    project.registry.get("st").pop("document")
    assert "Документ пока пуст" in project.document()
