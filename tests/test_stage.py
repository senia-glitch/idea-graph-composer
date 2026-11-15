"""Карта этапа: ворота, устаревание, помощник шага, ревью, правки зависимого, автоматическая очередь."""

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

def test_gates_approve_reopen_and_pending_cards(make):
    project = make(quiet, stage=True)
    with pytest.raises(EngineError) as info:
        project.approve_step("st", "roles")
    assert info.value.code == "empty_step"
    role = add(project, "роль", "Врач", status="proposed")
    assert steps(project)["roles"]["cards"] == 1 and steps(project)["roles"]["proposed"] == 1
    with pytest.raises(EngineError) as info:
        project.approve_step("st", "roles")
    assert info.value.code == "pending"
    project.approve_step("st", "roles", accept_all=True)
    assert project.store.cards[role]["status"] == "accepted" and steps(project)["roles"]["status"] == "approved"
    assert steps(project)["scen"]["status"] == "open"
    project.reopen_step("st", "roles")
    assert steps(project)["roles"]["status"] == "open"
    with pytest.raises(EngineError):
        project.approve_step("st", "нет-шага")
    with pytest.raises(EngineError):
        project.approve_step("clarify", "x")


def test_bank_step_gate_accepts_proposed_records(make):
    project = make(quiet, stage=True)
    bank = project.store.bank("comp")
    bank.put("competitor", {"name": "Докдок", "state": "proposed"}, "package:st")
    assert steps(project)["comp"]["records"] == 1 and steps(project)["comp"]["records_proposed"] == 1
    with pytest.raises(EngineError):
        project.approve_step("st", "comp")
    project.approve_step("st", "comp", accept_all=True)
    assert bank.list()[0]["data"]["state"] == "accepted" and steps(project)["comp"]["status"] == "approved"


# ---------------------------------------------------------------------- устаревание и правки зависимого

def test_editing_approved_step_marks_downstream_stale_and_agent_proposes_edits(make):
    seen = {"notes": [], "denied": []}

    def script(note, call, request):
        seen["notes"].append(note)
        seen["denied"].append(call("graph_apply", ops=[{"op": "update_card", "id": "c2", "title": "взлом"}]))
        seen["denied"].append(call("ask", question="?", options=["а", "б"]))
        call("graph_apply", ops=[{"op": "add_card", "kind": "правка", "title": "Правка: Сценарий записи",
                                  "body": "Поменять роль", "status": "proposed",
                                  "attrs": {"target": scenario, "field": "title", "new": "Сценарий записи у терапевта",
                                            "reason": "роль переименована"}}])
        call("finish", outcome="готово", summary="предложил правку")
        return ""

    project = make(script, stage=True)
    role = add(project, "роль", "Врач")
    scenario = add(project, "сценарий", "Сценарий записи")
    project.approve_step("st", "roles")
    project.human_ops([{"op": "update_card", "id": role, "title": "Терапевт"}])
    assert steps(project)["scen"]["stale"]["cards"] == [role] and steps(project)["roles"]["stale"] is None
    assert "Терапевт" not in steps(project)["scen"]["stale"]["reason"] and "Врач" in steps(project)["scen"]["stale"]["reason"]
    run = wait_for(lambda: project.runs.get("st"), ("finished", "error"))
    assert run.status == "finished" and run.role == "proposer"
    assert "Человек изменил утверждённые карточки" in seen["notes"][0] and "Сценарии" in seen["notes"][0]
    assert seen["denied"][0].startswith("ОТКАЗ role_limit") and seen["denied"][1].startswith("ОТКАЗ unknown_tool")
    proposal = next(c for c in project.store.cards.values() if c["kind"] == "правка")
    assert proposal["status"] == "proposed" and proposal["author"] == "package:st"
    project.apply_proposal(proposal["id"])
    assert project.store.cards[scenario]["title"] == "Сценарий записи у терапевта"
    assert proposal["id"] not in project.store.cards
    project.approve_step("st", "scen")                                     # утверждение снимает отметку
    assert steps(project)["scen"]["stale"] is None
    project.clear_stale("st", "scen")


def test_status_only_and_unapproved_changes_do_not_mark_stale(make):
    project = make(quiet, stage=True)
    role = add(project, "роль", "Врач", status="proposed")
    add(project, "сценарий", "Приём")
    project.human_ops([{"op": "update_card", "id": role, "title": "Терапевт"}])      # шаг не утверждён
    project.approve_step("st", "roles", accept_all=True)
    project.human_ops([{"op": "update_card", "id": role, "body": "тот же шаг без смены названия", "status": "accepted"}])
    assert steps(project)["scen"]["stale"] is not None                                # тело изменено — отметка есть
    project.clear_stale("st", "scen")
    project.human_ops([{"op": "update_card", "id": role, "status": "accepted"}])      # то же самое: ничего нового
    assert steps(project)["scen"]["stale"] is None
    project.human_ops([{"op": "delete_card", "id": role}])
    assert "удалена" in steps(project)["scen"]["stale"]["reason"]


def test_auto_propagate_can_be_switched_off_and_skips_busy_package(make):
    project = make(quiet, stage=True)
    project.settings.data["auto_propagate"] = False
    role = add(project, "роль", "Врач")
    add(project, "сценарий", "Приём")
    project.approve_step("st", "roles")
    project.human_ops([{"op": "update_card", "id": role, "title": "Терапевт"}])
    time.sleep(0.3)
    assert "st" not in project.runs and steps(project)["scen"]["stale"] is not None
    project.settings.data["auto_propagate"] = True
    project.clear_stale("st", "scen")
    project.start("st", fresh=True)                                      # диалог: ждёт ответа на вопрос
    from idea_graph_engine.runtime import Run
    project.runs["st"].question = {"id": "q", "question": "?", "options": [{"title": "а"}, {"title": "б"}]}
    project.runs["st"].status = "asking"
    wait_for(lambda: project.runs["st"], ("asking", "finished", "idle"))
    project.human_ops([{"op": "update_card", "id": role, "title": "Лор"}])
    time.sleep(0.3)
    assert project.runs["st"].role == ""                                  # правок не запускали: идёт диалог


def test_empty_dependent_steps_are_not_marked_stale(make):
    project = make(quiet, stage=True)
    project.settings.data["auto_propagate"] = False
    role = add(project, "роль", "Врач")
    project.approve_step("st", "roles")
    project.human_ops([{"op": "update_card", "id": role, "title": "Терапевт"}])
    assert steps(project)["scen"]["stale"] is None                        # у сценариев ещё ничего нет
    add(project, "сценарий", "Приём")
    project.human_ops([{"op": "update_card", "id": role, "title": "Лор"}])
    assert steps(project)["scen"]["stale"] is not None


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
        project.start("st", step="scen", action="review")
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


def test_step_with_requirement_instead_of_own_cards(make):
    project = make(quiet, stage=True)
    manifest = project.registry.get("st")
    manifest["stage"]["steps"][1]["requires"] = {"kind": "сценарий", "field": "Автоматизировать", "value": "да", "min": 1}
    scenario = add(project, "сценарий", "Запись", body="Автоматизировать: нет")
    assert steps(project)["scen"]["required"] == {"ok": False, "count": 0, "min": 1}
    with pytest.raises(EngineError) as info:
        project.approve_step("st", "scen")
    assert info.value.code == "empty_step" and "Автоматизировать: да" in info.value.message
    project.human_ops([{"op": "update_card", "id": scenario, "body": "Автоматизировать: нет\nАвтоматизировать: Да."}])
    assert steps(project)["scen"]["required"]["ok"] is True
    project.approve_step("st", "scen")
    assert steps(project)["scen"]["status"] == "approved"
