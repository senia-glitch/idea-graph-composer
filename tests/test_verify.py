"""Проверка перед выгрузкой: независимый проверяющий читает граф один раз за проход, вопросы и варианты правок строит код."""

import re

import pytest
from conftest import wait
from test_check import build_state
from test_flow import ASIS, TOBE, kinds

from idea_graph_engine import check
from idea_graph_engine.errors import EngineError
from idea_graph_engine.project import Run


def builder(note, call):
    """Помощник строит диаграммы при перестроении; в проверке он ничего не спрашивает."""
    assert "Автоматический режим" in note
    if "диаграмму «как есть»" in note:
        call("graph_apply", ops=[{"op": "add_card", "kind": "как есть", "title": "Как есть: Запись", "attrs": {"diagram": ASIS}}])
    elif "диаграмму «решение»" in note:
        call("graph_apply", ops=[{"op": "add_card", "kind": "решение", "title": "Решение: Запись", "attrs": {"diagram": TOBE}}])
    call("finish", summary="готово")


def checker_report(note, call, out_of_bounds=()):
    """Независимый проверяющий: один отчёт — смысловые пункты и ответы по пунктам кода."""
    assert "независимая проверка" in note and "Нужен ли платёжный модуль" in note          # границы переданы
    items = []
    for title in out_of_bounds:
        found = re.search(rf"«{title}» \((\w+)\)", note)
        if found:                                                                              # на контрольном проходе её в графе уже нет
            items.append({"kind": "boundary", "cards": [found.group(1)], "boundary": 1})
    answers = []
    for issue, text in re.findall(r"^(i\d+): (.*)$", note, re.M):
        ids = re.findall(r"\(([a-z]\d+)\)", text)
        if "решение не выбрано" in text:
            answers.append({"issue": issue, "pick": ids[-1] if "есть предложенные" in text else "suggest"})
        else:
            answers.append({"issue": issue, "pick": "remove"})
    result = call("report", items=items, answers=answers)
    assert result.startswith("Отчёт принят"), result


def make_agent(out_of_bounds=()):
    def agent(model, note, call, request):
        if model == "check":
            return checker_report(note, call, out_of_bounds)
        return builder(note, call)

    return agent


def checks(p):
    return [r for r in p.adapter.requests if r.model == "check"]


def test_manual_check_asks_code_built_questions_and_calls_model_once_per_pass(make, tmp_path):
    p = make(make_agent())
    build_state(p)
    p.start("export")
    asked = []
    while True:
        run = wait(p, ("asking", "done", "error"))
        if run.status != "asking":
            break
        q = run.question
        assert q["single"] is True and len(q["question"]) <= 120 and all(len(o["title"]) <= 60 and not o["consequence"] for o in q["options"])
        asked.append(q["question"])
        p.answer(q["id"], {"type": "choice", "indexes": [1]})
    assert run.status == "done", run.error
    assert asked == ["Нет решения: «Не дозвониться»", "Пусто: «Лишний»"]               # вопросы строит код; роль без сценария уже убрана
    assert len(checks(p)) == 2                                                          # проход с правками + один контрольный, не по вызову на вопрос
    assert [c["title"] for c in kinds(p, "роль")] == ["Клиент"]
    assert kinds(p, "вариант")[0]["status"] == "accepted" and len(kinds(p, "как есть")) == 1 and len(kinds(p, "решение")) == 1
    assert check.findings(p) == [] and run.total == 0                                   # никакого фиксированного «из N»
    assert "Исправлено правок: 2" in run.summary
    text = (tmp_path / "p" / "Документация.md").read_text(encoding="utf-8")
    assert "Запись на сайте" in text and "Лишний" not in text and "решение не выбрано" not in text


def test_auto_check_never_asks_and_removes_out_of_bounds(make, tmp_path):
    p = make(make_agent(out_of_bounds=("Оплата картой",)))
    scenario, _ = build_state(p)
    pay = p.act("add_problem", {"scenario": scenario["id"], "title": "Оплата картой", "body": "платёж", "importance": "средняя"})["id"]
    p.act("add_variant", {"problem": pay, "title": "Эквайринг", "body": "оплата на сайте"})          # выбран сразу
    p.start("export_auto")
    run = wait(p, ("asking", "done", "error"))
    assert run.status == "done", run.error                                                          # ни одного вопроса
    assert [c["title"] for c in kinds(p, "проблема")] == ["Не дозвониться"]                         # вне границ убрано вместе с решением
    assert kinds(p, "вариант")[0]["title"] == "Запись на сайте"
    assert len(checks(p)) == 2 and check.findings(p) == []
    assert "Автоматически исправлено правок: 3" in run.summary
    assert "Оплата" not in (tmp_path / "p" / "Документация.md").read_text(encoding="utf-8")


def test_plain_export_is_instant_and_needs_no_model(make, tmp_path):
    p = make(make_agent())
    build_state(p)
    path = p.act("export_md", {})["path"]
    assert path.endswith("Документация.md") and (tmp_path / "p" / "Документация.md").exists()
    assert checks(p) == [] and p.run is None                                                       # без проверки и без модели


def test_checker_is_independent_and_report_is_validated(make):
    p = make(lambda *a: None)
    scenario, problem = build_state(p)
    p.settings.data["checker"] = dict(p.settings.data["autopilot"])
    run = Run("export", "t")
    p._checker_slot(run)
    assert "не независима" in run.notes[0] and "Автопилот" in run.notes[0]                          # та же модель, что строит
    p.settings.data["checker"] = {"agent": "script", "model": "check", "effort": ""}
    fresh = Run("export", "t")
    p._checker_slot(fresh)
    assert not fresh.notes

    found = check.issues(p)
    p.tokens["t"] = {"kind": "check", "run": run, "note": "", "report": None, "bounds": ["Нет оплаты"], "found": found}

    def refused(**args):
        with pytest.raises(EngineError) as error:
            p.tool_call("t", "report", args)
        return error.value.message

    assert "нет ответа по пункту" in refused(items=[], answers=[])                                  # по пунктам кода обязателен ответ
    assert "boundary — номер границы" in refused(items=[{"kind": "boundary", "cards": [scenario["id"]], "boundary": 7}], answers=[])
    assert "нужно ровно 2 разных" in refused(items=[{"kind": "duplicate", "cards": [scenario["id"]], "remove": scenario["id"]}], answers=[])
    assert "kind — одно из" in refused(items=[{"kind": "по-моему странно", "cards": [scenario["id"]]}], answers=[])
    assert "нет карточки" in refused(items=[{"kind": "boundary", "cards": ["zzz"], "boundary": 1}], answers=[])
    answers = [{"issue": i["id"], "pick": "remove"} for i in found if i["code"] == "empty"]
    answers += [{"issue": i["id"], "pick": "не-вариант"} for i in found if i["code"] == "no_decision"]
    assert "pick — id одного из предложенных" in refused(items=[], answers=answers)


def test_check_stops_instead_of_looping_when_checker_never_reports(make):
    p = make(lambda model, note, call, request: None)                  # проверяющий ничего не сдаёт
    build_state(p)
    p.start("export_auto")
    run = wait(p, ("done", "error"))
    assert run.status == "error" and "не сдал отчёт" in run.error
