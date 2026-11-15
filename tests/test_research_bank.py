"""Исследователи (параллельно, на разных моделях), банки проекта и настройки ролей."""

import json

from conftest import wait_for

from idea_graph_core import HUMAN
from idea_graph_engine.config import Settings


def sphere(project):
    return project.store.apply([{"op": "add_card", "kind": "сфера", "title": "Клиники"}], HUMAN)["ids"][0]


def researcher_reply(call, name="Докдок"):
    call("report", findings=[{"kind": "конкурент", "title": name, "data": {"url": "https://a.example"},
                              "sources": ["https://a.example"]}], summary="ок")


def test_replicate_mode_runs_every_task_on_every_slot_and_feeds_main_agent(make):
    seen = {"researchers": [], "web": [], "main": [], "bank": []}

    def script(note, call, request):
        if note.startswith("ИССЛ"):
            seen["researchers"].append((request.model, note.split()[1]))
            seen["web"].append(request.web_search)
            assert call("turn_info") == note
            assert call("graph_apply", ops=[]).startswith("ОТКАЗ unknown_tool")
            researcher_reply(call)
            assert call("report", findings=[{"title": "ещё", "sources": ["u"]}]).startswith("ОТКАЗ one_report")
            return ""
        seen["main"].append(note)
        seen["bank"].append(call("bank_write", name="comp", records=[
            {"type": "competitor", "key": "a.example", "data": {"name": "Докдок"}, "sources": ["https://a.example"]}]))
        seen["bank"].append(call("bank_write", name="other", records=[{"type": "t", "data": {}}]))
        seen["bank"].append(call("bank_read", name="comp"))
        seen["bank"].append(call("bank_read", name="secret"))
        call("finish", outcome="готово", summary="записал")
        return ""

    project = make(script, stage=True)
    (project.dir / "resources.json").write_text('{"next": 1, "items": []}', encoding="utf-8")
    target = sphere(project)
    project.start("st", step="comp", action="suggest", target=target)
    run = wait_for(lambda: project.runs["st"], ("finished", "error"), timeout=15)
    assert run.status == "finished"
    assert sorted(seen["researchers"]) == sorted((f"r{i}", t) for i in (1, 2, 3) for t in ("задача-1", "задача-2"))
    assert all(seen["web"]) and "СВОДКА задача-1:3; задача-2:3" in seen["main"][0]
    assert seen["bank"][0].startswith("Записано в банк «comp»") and seen["bank"][1].startswith("ОТКАЗ bank_denied")
    assert "Докдок" in seen["bank"][2] and "https://a.example" in seen["bank"][2] and seen["bank"][3].startswith("ОТКАЗ bank_denied")
    record = project.store.bank("comp").list()[0]
    assert record["data"] == {"name": "Докдок", "state": "proposed"} and record["author"] == "package:st"
    saved = json.loads((project.dir / "packages" / "st.json").read_text(encoding="utf-8"))
    reports = saved["research"]["comp"]["tasks"]
    assert [t["title"] for t in reports] == ["задача-1", "задача-2"] and len(reports[0]["reports"]) == 3
    assert reports[0]["reports"][0]["findings"][0]["data"] == {"url": "https://a.example"} and run.cost > 0


def test_distribute_mode_spreads_tasks_over_slots(make):
    models = []

    def script(note, call, request):
        if note.startswith("ИССЛ"):
            models.append((request.model, note.split()[1]))
            researcher_reply(call)
            return ""
        call("finish", outcome="готово", summary="ок")
        return ""

    project = make(script, stage=True)
    project.registry.get("st")["stage"]["steps"][2]["research_mode"] = "distribute"
    project.start("st", step="comp", action="suggest")
    run = wait_for(lambda: project.runs["st"], ("finished", "error"), timeout=15)
    assert run.status == "finished" and sorted(models) == [("r1", "задача-1"), ("r2", "задача-2")]


def test_research_fails_when_nobody_reports_and_report_is_validated(make):
    statuses, replies = [], []

    def script(note, call, request):
        if note.startswith("ИССЛ"):
            statuses.append(project.runs["st"].status)
            replies.append(call("report", findings=[{"kind": "x"}, "строка"]))
            return ""
        raise AssertionError("основной агент не должен запускаться без исследования")

    project = make(script, stage=True)
    project.start("st", step="comp", action="suggest")
    run = wait_for(lambda: project.runs["st"], ("error",), timeout=15)
    assert "не вернули результатов" in run.error and set(statuses) == {"researching"}
    assert all(r.startswith("ОТКАЗ empty_report") for r in replies)


def test_research_is_optional_in_auto_mode(make):
    notes = []

    def script(note, call, request):
        if note.startswith("ИССЛ"):
            researcher_reply(call)
            return ""
        notes.append(note)
        call("finish", outcome="готово", summary="ок")
        return ""

    project = make(script, stage=True)
    one = sphere(project)
    project.settings.data["auto_research"] = False
    project.start("st", auto={"targets": [one]})
    wait_for(lambda: project.runs["st"], ("finished",), timeout=15)
    assert not any("СВОДКА" in n for n in notes)
    project.settings.data["auto_research"] = True
    project.start("st", auto={"targets": [one]})
    wait_for(lambda: project.runs["st"], ("finished",), timeout=15)
    assert any("СВОДКА" in n for n in notes)
    assert project.runs["st"].autorun["research"] is True


def test_bank_tools_validate_arguments(make):
    out = []

    def script(note, call, request):
        out.append(call("bank_write", name="comp", records=[]))
        out.append(call("bank_write", name="comp", records=[{"type": "t", "data": "не объект"}]))
        out.append(call("bank_write", name="comp", records=json.dumps([{"type": "t", "data": json.dumps({"a": 1})}])))
        out.append(call("bank_read", name="comp", type="t"))
        out.append(call("bank_read", name="comp", key="нет"))
        call("finish", outcome="готово", summary="ок")
        return ""

    project = make(script, stage=True)
    project.start("st")
    wait_for(lambda: project.runs["st"], ("finished",))
    assert out[0].startswith("ОТКАЗ invalid_records") and out[1].startswith("ОТКАЗ invalid_records")
    assert out[2].startswith("Записано") and '"a": 1' in out[3] and "записей нет" in out[4]


def test_settings_roles_and_flags(tmp_path):
    settings = Settings(tmp_path / "s.json")
    assert len(settings.data["researchers"]) == 3 and len(settings.data["advisers"]) == 3
    assert settings.data["reviewer"]["agent"] == "claude" and settings.data["auto_propagate"] is True
    settings.update({"advisers": [{"agent": "opencode", "model": "m1"}, {"agent": "claude"}, {"agent": "claude", "effort": "high"}],
                     "reviewer": {"agent": "opencode", "model": "x/y", "effort": "max"},
                     "auto_propagate": 0, "auto_research": 0, "auto_unlimited": 1,
                     "researchers": [{"agent": "claude"}]})                      # неверная длина — игнор
    data = Settings(tmp_path / "s.json").data                                     # сохранилось на диск
    assert data["advisers"][0] == {"agent": "opencode", "model": "m1", "effort": ""}
    assert data["advisers"][2]["effort"] == "high" and data["reviewer"] == {"agent": "opencode", "model": "x/y", "effort": "max"}
    assert data["auto_propagate"] is False and data["auto_research"] is False and data["auto_unlimited"] is True
    assert len(data["researchers"]) == 3 and data["researchers"][0]["model"] == "opus"
    settings.update({"auto_limits": {"st": {"n": 99, "x": "zz"}}, "projects_root": " /tmp/x ", "evil": 1})
    assert settings.data["auto_limits"] == {"st": {"n": 20}} and settings.data["projects_root"] == "/tmp/x"
    assert "evil" not in settings.data
