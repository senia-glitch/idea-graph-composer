"""Цепочка целиком: исследователи, банк, роли, сценарии, проблемы, диаграммы, решение."""

import pytest
from conftest import wait

from idea_graph_engine.errors import EngineError

ASIS = {"participants": ["Клиент", "Администратор"], "steps": [
    {"from": "Клиент", "to": "Администратор", "text": "Звонит и просит запись", "pain": {"what": "Не дозвониться"}},
    {"from": "Администратор", "to": "Клиент", "text": "Называет время"}]}
TOBE = {"participants": ["Клиент", "Система"], "steps": [
    {"from": "Клиент", "to": "Система", "text": "Выбирает время на сайте", "mark": "new"},
    {"from": "Система", "to": "Клиент", "text": "Подтверждает запись", "mark": "auto"}]}


def script(model, note, call, request):
    if model.startswith("r"):
        if "Проанализируй" in note:
            call("report", findings=[
                {"kind": "роль", "title": "Клиент", "description": "записывается", "data": {"competitor": "https://www.a.ru/"}, "sources": ["https://a.ru/zapis"]},
                {"kind": "процесс", "title": "Онлайн-запись", "data": {"competitor": "a.ru"}, "sources": ["https://a.ru"]},
                {"kind": "роль", "title": "Чужой", "data": {"competitor": "zzz.ru"}, "sources": ["https://zzz.ru"]}])
        else:
            call("report", findings=[
                {"kind": "конкурент", "title": "Салон А", "description": "стрижки", "data": {"url": "https://a.ru"}, "sources": ["https://a.ru"]},
                {"kind": "конкурент", "title": "Салон Б", "description": "стрижки", "data": {"url": "https://www.b.ru/"}, "sources": ["https://b.ru"]}])
        return
    if "предложить роли" in note:
        if "Ответ человека" not in note:
            call("ask", question="Включать ли внешние системы?", options=[{"title": "Да"}, {"title": "Нет"}], recommended=[2])
            return
        call("graph_apply", ops=[{"op": "add_card", "kind": "роль", "title": "Клиент", "body": "записывается"},
                                 {"op": "add_card", "kind": "роль", "title": "Администратор"}])
    elif "предложить сценарии" in note:
        bad = call("graph_apply", ops=[{"op": "add_card", "kind": "сценарий", "title": "Плохой",
                                        "attrs": {"usecase": {"cases": [{"name": "х", "actors": ["Робот"]}]}}}])
        assert bad.startswith("ОТКАЗ bad_actor")
        call("graph_apply", ops=[{"op": "add_card", "kind": "сценарий", "title": "Запись на приём", "body": "клиент записывается",
                                  "attrs": {"usecase": {"system": "Запись", "cases": [{"name": "Выбрать время", "actors": ["Клиент"]},
                                                                                   {"name": "Подтвердить", "actors": ["Клиент", "Администратор"]}]}}}])
    elif "ЗАДАЧА: предложить проблемы" in note:
        call("graph_apply", ops=[{"op": "add_card", "kind": "проблема", "title": "Не дозвониться", "body": "часто", "attrs": {"importance": "Высокая"}},
                                 {"op": "add_card", "kind": "проблема", "title": "Забывают прийти"}])
    elif "диаграмму «как есть»" in note:
        assert "Не дозвониться" in note and "Забывают прийти" not in note          # только принятые проблемы
        call("graph_apply", ops=[{"op": "add_card", "kind": "как есть", "title": "Как есть: Запись на приём", "attrs": {"diagram": ASIS}}])
    elif "варианты автоматизации" in note:
        call("graph_apply", ops=[{"op": "add_card", "kind": "вариант", "title": "Запись на сайте", "body": "форма"},
                                 {"op": "add_card", "kind": "вариант", "title": "Бот"}])
    elif "диаграмму «решение»" in note:
        assert "Запись на сайте" in note and "Звонит и просит запись" in note
        call("graph_apply", ops=[{"op": "add_card", "kind": "решение", "title": "Решение: Запись на приём", "attrs": {"diagram": TOBE}}])
    call("finish", summary="готово")


def kinds(project, kind):
    return [c for c in project.store.snapshot()["cards"] if c["kind"] == kind]


def test_whole_chain(make):
    p = make(script)
    with pytest.raises(EngineError):
        p.start("competitors")                                                    # без сферы нельзя
    p.act("set_sphere", {"title": "парикмахерская"})
    p.act("bank_add", {"type": "requirement", "text": "Нужна интеграция с 1С"})

    # конкуренты: три исследователя, дубли склеены, отклонённые не возвращаются
    p.start("competitors")
    assert wait(p).status == "done"
    comps = p.records("competitor")
    assert sorted(r["key"] for r in comps) == ["a.ru", "b.ru"] and all(r["data"]["state"] == "proposed" for r in comps)
    assert "Нужна интеграция с 1С" in p.adapter.requests[0].note
    a = next(r for r in comps if r["key"] == "a.ru")
    p.act("bank_update", {"id": a["id"], "state": "accepted"})
    b = next(r for r in comps if r["key"] == "b.ru")
    p.act("bank_remove", {"id": b["id"]})
    p.start("competitors")
    wait(p)
    assert [r["key"] for r in p.records("competitor")] == ["a.ru"]               # б отклонён, а уже есть

    # анализ: факты только по принятым, повторно — только новых
    p.start("analysis")
    assert wait(p).status == "done"
    facts = p.records("fact")
    assert sorted(f["data"]["text"].split(".")[0] for f in facts) == ["Клиент", "Онлайн-запись"]
    assert all(f["data"]["competitor"] == "a.ru" for f in facts) and p.records("competitor")[0]["data"]["analyzed"]
    with pytest.raises(EngineError):
        p.start("analysis")                                                         # новых нет
    p.start("analysis", redo=True)
    wait(p)
    assert len(p.records("fact")) == 2

    # роли: помощник задаёт вопрос, после ответа предлагает
    p.start("roles")
    run = wait(p)
    assert run.status == "asking" and run.question["question"].startswith("Включать")
    p.answer(run.question["id"], {"type": "choice", "indexes": [2]})
    assert wait(p, ("done", "error")).status == "done"
    assert "Ответ человека на твой вопрос" in p.adapter.requests[-1].note and "Нет" in p.adapter.requests[-1].note
    assert "Онлайн-запись" in p.adapter.requests[-2].note                            # помощник видит банк
    roles = kinds(p, "роль")
    assert [r["status"] for r in roles] == ["proposed", "proposed"]
    for r in roles:
        p.act("accept", {"id": r["id"]})
    p.act("add_role", {"title": "Мастер"})
    with pytest.raises(EngineError):
        p.act("add_role", {"title": "мастер"})

    # сценарии: диаграмма вариантов использования, связи с ролями
    p.start("scenarios")
    assert wait(p).status == "done"
    scenario = kinds(p, "сценарий")[0]
    assert [c["name"] for c in scenario["attrs"]["usecase"]["cases"]] == ["Выбрать время", "Подтвердить"]
    assert sorted(r["title"] for r in p.roles_of(scenario["id"])) == ["Администратор", "Клиент"]
    p.act("accept", {"id": scenario["id"]})

    # как есть: сначала проблемы, потом диаграмма
    with pytest.raises(EngineError):
        p.start("asis")
    p.start("problems")
    wait(p)
    problems = kinds(p, "проблема")
    assert problems[0]["attrs"]["importance"] == "высокая"
    p.act("accept", {"id": problems[0]["id"]})
    p.act("remove", {"id": problems[1]["id"]})
    p.start("asis")
    assert wait(p).status == "done"
    asis = kinds(p, "как есть")[0]
    assert asis["status"] == "accepted" and asis["attrs"]["diagram"]["steps"][0]["pain"]["what"] == "Не дозвониться"

    # решение: варианты → выбор одного → диаграмма
    with pytest.raises(EngineError):
        p.start("solution")
    p.start("variants")
    wait(p)
    variants = kinds(p, "вариант")
    p.act("choose_variant", {"id": variants[0]["id"]})
    assert [v["title"] for v in kinds(p, "вариант")] == ["Запись на сайте"]
    p.start("solution")
    assert wait(p).status == "done"
    assert kinds(p, "решение")[0]["attrs"]["diagram"]["steps"][0]["mark"] == "new"
    p.start("solution")                                                             # перестроение заменяет, а не дублирует
    wait(p)
    assert len(kinds(p, "решение")) == 1


def test_human_edits_and_cascades(make):
    p = make(lambda *a: None)
    p.act("set_sphere", {"title": "парикмахерская"})
    p.act("set_sphere", {"title": "барбершоп"})
    assert [c["title"] for c in kinds(p, "сфера")] == ["барбершоп"]
    with pytest.raises(EngineError):
        p.act("remove", {"id": kinds(p, "сфера")[0]["id"]})
    p.act("add_role", {"title": "Клиент"})
    p.act("add_role", {"title": "Мастер"})
    p.act("add_scenario", {"title": "Стрижка", "roles": ["Клиент", "мастер"]})
    scenario = kinds(p, "сценарий")[0]
    assert scenario["status"] == "accepted" and scenario["attrs"]["usecase"]["cases"][0]["actors"] == ["Клиент", "Мастер"]
    p.act("save_steps", {"scenario": scenario["id"], "kind": "как есть", "text": "1. Клиент → Мастер: Приходит\n2. Мастер → Клиент: Стрижёт [боль: долго | всегда | час | высокая]"})
    diagram = kinds(p, "как есть")[0]["attrs"]["diagram"]
    assert diagram["steps"][1]["pain"]["what"] == "долго"
    p.act("save_steps", {"scenario": scenario["id"], "kind": "как есть", "text": "1. Клиент → Мастер: Приходит\n2. Мастер → Клиент: Стрижёт"})
    assert len(kinds(p, "как есть")) == 1 and "pain" not in kinds(p, "как есть")[0]["attrs"]["diagram"]["steps"][1]
    with pytest.raises(EngineError):
        p.act("save_steps", {"scenario": scenario["id"], "kind": "как есть", "text": "ерунда"})
    master = next(r for r in kinds(p, "роль") if r["title"] == "Мастер")
    p.act("remove", {"id": master["id"]})                                          # актёр уходит из диаграммы сценария
    assert kinds(p, "сценарий")[0]["attrs"]["usecase"]["cases"][0]["actors"] == ["Клиент"]
    p.act("remove", {"id": scenario["id"]})                                        # и диаграммы под сценарием
    assert not kinds(p, "как есть") and not kinds(p, "сценарий")
    assert p.rejected("сценарий") == ["стрижка"]
    p.act("bank_add", {"type": "competitor", "name": "Салон", "url": "https://www.salon.ru/"})
    p.act("bank_add", {"type": "fact", "text": "Есть запись онлайн"})
    record = p.records("competitor")[0]
    assert record["key"] == "salon.ru" and record["data"]["state"] == "accepted"


def test_agent_failure_is_shown(make):
    def failing(model, note, call, request):
        raise RuntimeError("упал")

    p = make(failing)
    p.act("set_sphere", {"title": "x"})
    p.start("roles")
    run = wait(p)
    assert run.status == "error" and "упал" in run.error
