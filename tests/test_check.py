"""Проверка согласованности перед выгрузкой: разногласия исправляются в графе по диалогу, документ — отражение графа."""

import re

from conftest import wait
from test_flow import ASIS, TOBE, kinds

from idea_graph_engine import check


def build_state(p):
    """Клиент и Лишний (без описания и без сценария); сценарий с проблемой, у которой решение не выбрано."""
    p.act("set_sphere", {"title": "парикмахерская"})
    p.act("bank_add", {"type": "requirement", "text": "Граница системы: Нужен ли платёжный модуль? → Нет"})
    p.act("add_role", {"title": "Клиент", "body": "записывается"})
    p.act("add_role", {"title": "Лишний"})
    p.act("add_scenario", {"title": "Запись", "body": "клиент записывается", "roles": ["Клиент"]})
    for card in p.store.snapshot()["cards"]:
        if card["kind"] in ("роль", "сценарий"):
            p.act("accept", {"id": card["id"]})
    scenario = kinds(p, "сценарий")[0]
    problem = p.act("add_problem", {"scenario": scenario["id"], "title": "Не дозвониться", "body": "часто", "importance": "высокая"})["id"]
    p.act("accept", {"id": problem})
    variant = p.act("add_variant", {"problem": problem, "title": "Запись на сайте", "body": "форма"})["id"]
    p._human([{"op": "update_card", "id": variant, "status": "proposed"}])        # предложено, но не выбрано
    return scenario, problem


def test_findings_are_exact(make):
    p = make(lambda *a: None)
    build_state(p)
    text = "\n".join(check.findings(p))
    assert "Лишний" in text and "нет описания" in text and "не участвует ни в одном принятом сценарии" in text
    assert "принята, а решение не выбрано; есть предложенные варианты: «Запись на сайте»" in text
    assert "нет диаграммы «как есть»" in text


def agent(model, note, call, request):
    if model != "pilot":                                                     # помощник: диаграммы при перестроении
        assert "Автоматический режим" in note                                # в проверке он ничего не спрашивает
        if "диаграмму «как есть»" in note:
            call("graph_apply", ops=[{"op": "add_card", "kind": "как есть", "title": "Как есть: Запись", "attrs": {"diagram": ASIS}}])
        elif "диаграмму «решение»" in note:
            call("graph_apply", ops=[{"op": "add_card", "kind": "решение", "title": "Решение: Запись", "attrs": {"diagram": TOBE}}])
        call("finish", summary="готово")
        return
    assert "Граница системы" not in note or "Нужен ли платёжный модуль" in note           # границы переданы агенту
    issues = re.findall(r"^\d+\. (.*)$", note.split("Найдено проверкой кодом")[1].split("Уже решено")[0], re.M)
    if not issues:
        call("finish", summary="разногласий нет")
        return
    first = issues[0]
    ids = re.findall(r"\(([a-z]\d+)\)", first)
    if first.startswith("Роль"):
        bad = call("conflict", question="Лишняя роль", options=[{"title": "Оставить"}, {"title": "Убрать", "fix": {"do": "remove", "id": ids[0]}}])
        assert bad.startswith("ОТКАЗ bad_fix")                                # варианта «оставить» (без правки) быть не может
        assert call("finish", summary="рано").startswith("ОТКАЗ not_finished")  # пока код видит разногласия, закончить нельзя
        call("conflict", question="Роль «Лишний» не нужна ни одному сценарию", recommended=[1],
             options=[{"title": "Убрать роль", "fix": {"do": "remove", "id": ids[0]}},
                      {"title": "Описать роль", "fix": {"do": "edit", "id": ids[0], "body": "делает что-то"}}])
    elif "решение не выбрано" in first:
        call("conflict", question="Какое решение берём?", options=[
            {"title": "Запись на сайте", "fix": {"do": "choose_variant", "id": ids[-1]}},
            {"title": "Убрать проблему", "fix": {"do": "remove", "id": ids[0]}}])
    else:                                                                    # нет диаграмм
        call("conflict", question="Диаграммы не построены", options=[
            {"title": "Построить заново", "fix": {"do": "rebuild", "id": ids[0]}},
            {"title": "Убрать сценарий", "fix": {"do": "remove", "id": ids[0]}}])


def test_export_asks_fixes_graph_then_writes(make, tmp_path):
    p = make(agent)
    build_state(p)
    p.start("export")
    asked = []
    for pick in (1, 1, 1, None):                                              # убрать роль, выбрать решение, перестроить диаграммы
        run = wait(p, ("asking", "done", "error"))
        if pick is None:
            break
        assert run.status == "asking" and run.question["single"] is True       # один вопрос, один ответ, без «оставить» и «ни один»
        asked.append(run.question["question"])
        p.answer(run.question["id"], {"type": "choice", "indexes": [pick]})
    assert run.status == "done", run.error
    assert len(asked) == 3
    assert [c["title"] for c in kinds(p, "роль")] == ["Клиент"]               # правка применена в графе
    assert [c["title"] for c in kinds(p, "вариант")] == ["Запись на сайте"] and kinds(p, "вариант")[0]["status"] == "accepted"
    assert len(kinds(p, "как есть")) == 1 and len(kinds(p, "решение")) == 1
    assert kinds(p, "как есть")[0]["attrs"].get("basis")                     # диаграмма помнит, по чему построена
    assert check.findings(p) == []
    assert "Исправлено разногласий: 3" in run.summary
    text = (tmp_path / "p" / "Документация.md").read_text(encoding="utf-8")
    assert "Запись на сайте" in text and "Лишний" not in text and "решение не выбрано" not in text


def test_changed_problems_make_diagrams_stale(make):
    p = make(agent)
    scenario, problem = build_state(p)
    p.act("remove", {"id": kinds(p, "роль")[1]["id"]})
    p.act("choose_variant", {"id": kinds(p, "вариант")[0]["id"]})
    p.start("export")
    for _ in range(3):
        run = wait(p, ("asking", "done", "error"))
        if run.status != "asking":
            break
        p.answer(run.question["id"], {"type": "choice", "indexes": [1]})
    assert wait(p, ("done", "error")).status == "done"
    extra = p.act("add_problem", {"scenario": scenario["id"], "title": "Забывают прийти", "body": "бывает", "importance": "средняя"})["id"]
    p.act("accept", {"id": extra})
    text = "\n".join(check.findings(p))
    assert "Забывают прийти" in text and "решение не выбрано" in text           # новая принятая проблема без решения
    assert "«как есть» не соответствует принятым проблемам" in text and "построена по старым" in text
