"""Ход пакета: вопрос с вариантами, ответ человека, сообщения, остановка."""

import json

import pytest
from conftest import wait_for

from idea_graph_core import ACCEPTED, HUMAN
from idea_graph_engine.runtime import EngineError


def clarify_script(note, call, request):
    if "Начало работы" in note:
        assert "Граф целиком" in note and "[c1] ветка" in note
        call("ask", question="Кто пользователь?", cards=["c1"], recommended=1, reason="их больше",
             options=[{"title": "Пациенты", "consequence": "нужен простой вход"}, {"title": "Регистраторы"}])
        assert call("ask", question="ещё?", options=["а", "б"]).startswith("ОТКАЗ one_question")
        return ""
    if "Ответ на твой вопрос" in note:
        assert "Ответил человек" in note
        call("graph_apply", ops=[{"op": "add_card", "kind": "решение", "title": "Пациенты", "parent": "c1",
                                  "status": "accepted", "decision": "last"}])
        call("finish", outcome="готово", summary="Выбрали пациентов")
    return ""


def test_human_flow(make):
    project = make(clarify_script)
    project.start("clarify")
    run = wait_for(lambda: project.runs.get("clarify"), ("asking",))
    question = run.question
    assert question["question"] == "Кто пользователь?" and question["recommended"] == [1]
    project.answer("clarify", question["id"], {"type": "choice", "indexes": [1]})
    run = wait_for(lambda: project.runs["clarify"], ("finished",))
    assert run.outcome == "готово" and run.summary == "Выбрали пациентов" and run.cost > 0
    card = project.store.cards["c2"]
    assert card["status"] == ACCEPTED and card["decision"]["by"] == HUMAN
    assert card["decision"]["chosen"]["value"] == "Пациенты" and card["decision"]["options"] == ["Пациенты", "Регистраторы"]
    kinds = [m["kind"] for m in project.chat]
    assert "question" in kinds and "answer" in kinds and "finish" in kinds


def test_several_options_and_own_text(make):
    def script(note, call, request):
        if "Начало работы" in note:
            call("ask", question="Что учесть?", options=["А", "Б", "В"])
            return ""
        call("finish", outcome="готово", summary="ок")
        return ""

    project = make(script)
    project.start("clarify")
    run = wait_for(lambda: project.runs.get("clarify"), ("asking",))
    project.answer("clarify", run.question["id"], {"type": "choice", "indexes": [3, 1, 3], "text": "и ещё Г"})
    wait_for(lambda: project.runs["clarify"], ("finished",))
    assert "выбраны варианты 1, 3" in project.chat[-2]["text"] or "Выбраны варианты 1, 3" in project.chat[-2]["text"]


def test_answer_is_checked(make):
    clean = make(clarify_script)._clean_choice
    question = {"options": [{"title": "А"}, {"title": "Б"}]}
    assert clean(question, {"type": "choice", "indexes": [2]})["values"] == ["Б"]
    assert clean(question, {"type": "choice", "text": "своё"})["value"] == "своё"
    assert clean(question, {"type": "rejected"}) == {"type": "rejected", "value": ""}
    for bad in ({"type": "choice"}, {"type": "choice", "indexes": [3]}, {"type": "choice", "indexes": ["x"]},
                {"type": "option", "index": 1}):
        with pytest.raises(EngineError):
            clean(question, bad)


def test_message_withdraws_open_question_and_stop(make):
    seen = []

    def script(note, call, request):
        seen.append(note)
        if "Начало работы" in note:
            call("ask", question="Кто?", options=["а", "б"])
        elif "Сообщение человека" in note:
            call("finish", outcome="готово", summary="учёл")
        return ""

    project = make(script)
    project.start("clarify")
    wait_for(lambda: project.runs.get("clarify"), ("asking",))
    project.message("clarify", "Лучше про врачей")
    run = wait_for(lambda: project.runs["clarify"], ("finished",))
    assert run.question is None and "Лучше про врачей" in seen[-1] and "снят" in seen[-1]
    with pytest.raises(EngineError):
        project.message("clarify", "  ")
    with pytest.raises(EngineError):
        project.start("нет-такого")


def test_failed_turn_keeps_events_and_reports_error(make, monkeypatch):
    project = make(lambda note, call, request: "")
    from idea_graph_engine.agents import TurnResult
    monkeypatch.setattr(project.adapters.get("script"), "run", lambda request: TurnResult(error="лимит исчерпан"))
    project.start("clarify")
    run = wait_for(lambda: project.runs["clarify"], ("error",))
    assert "лимит исчерпан" in run.error and run.inbox and "Начало работы" in run.inbox[0]


def test_human_ops_and_undo_touch_state(make):
    project = make(lambda note, call, request: "")
    seq = project.seq
    project.human_ops([{"op": "add_card", "kind": "ветка", "title": "Новая", "parent": "c1"}])
    assert project.seq > seq and "c2" in project.store.cards
    project.undo(project.store.revision)
    assert "c2" not in project.store.cards


def test_run_state_survives_reopen(make, tmp_path):
    project = make(clarify_script)
    project.start("clarify")
    wait_for(lambda: project.runs.get("clarify"), ("asking",))
    from idea_graph_engine.runtime import Project
    again = Project(tmp_path / "idea", project.settings, project.registry, project.adapters)
    assert again.runs["clarify"].question["question"] == "Кто пользователь?"
    assert json.loads((again.dir / "runs" / "clarify.json").read_text(encoding="utf-8"))["package"] == "clarify"
