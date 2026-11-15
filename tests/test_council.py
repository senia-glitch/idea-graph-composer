"""Совет из трёх агентов в автоматическом режиме."""

from conftest import wait_for

from idea_graph_core import HUMAN
from idea_graph_engine import council


def pos(n, indexes, rationale="потому что", values=None):
    values = values or [f"в{i}" for i in indexes]
    return {"n": n, "rationale": rationale, "value": "…",
            "chosen": {"type": "choice", "indexes": indexes, "values": values, "text": "", "value": "; ".join(values)}}


def test_tally_majority_partial_overlap_and_chair():
    chosen, winners, agreed = council.tally([pos(1, [1]), pos(2, [1]), pos(3, [2])])
    assert chosen["indexes"] == [1] and agreed and [w["n"] for w in winners] == [1, 2]
    chosen, winners, agreed = council.tally([pos(1, [1, 2]), pos(2, [2, 3]), pos(3, [3])])
    assert chosen["indexes"] == [2, 3] and agreed and chosen["values"] == ["в2", "в3"]
    chosen, winners, agreed = council.tally([pos(1, [1]), pos(2, [2]), pos(3, [3])])
    assert chosen["indexes"] == [1] and not agreed and [w["n"] for w in winners] == [1]
    rejected = {"n": 2, "rationale": "r", "value": "x", "chosen": {"type": "rejected", "value": ""}}
    rejected3 = {"n": 3, "rationale": "r", "value": "x", "chosen": {"type": "rejected", "value": ""}}
    chosen, _, agreed = council.tally([pos(1, [1]), rejected, rejected3])
    assert chosen["type"] == "rejected" and agreed


def sphere(project):
    return project.store.apply([{"op": "add_card", "kind": "сфера", "title": "Клиники"}], HUMAN)["ids"][0]


def make_script(votes, round2_votes=None, ask_calls=None):
    """Основной агент задаёт один вопрос и записывает решение совета; советники голосуют по списку votes."""
    counts = {"advisers": 0}

    def script(note, call, request):
        if request.model.startswith("a"):
            counts["advisers"] += 1
            second = "Позиции советников" in note
            table = (round2_votes or votes) if second else votes
            vote = table[request.model]
            if vote is None:
                return ""                                   # советник не ответил
            call("decide", choice="choice", options=vote, rationale=f"{request.model}: " + ("раунд 2" if second else "раунд 1"))
            return ""
        if "ПРЕДЛОЖИ-РОЛИ" in note and "Ответ на твой вопрос" not in note:
            call("ask", question="Какие роли?", options=["Врач", "Пациент", "Регистратор"])
            return ""
        if "Ответ на твой вопрос" in note:
            if ask_calls is not None:
                ask_calls.append(note)
            call("graph_apply", ops=[{"op": "add_card", "kind": "роль", "title": "Роль по решению совета",
                                      "decision": "last"}])
        call("finish", outcome="готово", summary="шаг готов")
        return ""

    script.counts = counts
    return script


def run_auto(project, sphere_id):
    project.start("st", auto={"targets": [sphere_id], "research": False})
    return wait_for(lambda: project.runs.get("st"), ("finished", "error", "asking"), timeout=15)


def test_unanimous_council_skips_discussion(make):
    script = make_script({"a1": [1], "a2": [1], "a3": [1]})
    project = make(script, stage=True)
    run = run_auto(project, sphere(project))
    assert run.status == "finished" and script.counts["advisers"] == 3
    card = next(c for c in project.store.cards.values() if c["title"] == "Роль по решению совета")
    decision = card["decision"]
    assert decision["by"] == "council" and decision["chosen"]["indexes"] == [1] and not decision["disputed"]
    assert decision["rationale"].startswith("a1: раунд 1") and "Особое мнение" not in decision["rationale"]
    assert [p["n"] for p in decision["positions"]] == [1, 2, 3]
    assert project.chat[[m["kind"] for m in project.chat].index("answer")]["who"] == "council"


def test_disagreement_triggers_one_round_and_dissent_goes_into_rationale(make):
    script = make_script({"a1": [1], "a2": [1], "a3": [2]})
    project = make(script, stage=True)
    run = run_auto(project, sphere(project))
    assert run.status == "finished" and script.counts["advisers"] == 6              # два круга по трое
    decision = next(c for c in project.store.cards.values() if c["kind"] == "роль")["decision"]
    assert decision["chosen"]["indexes"] == [1] and decision["disputed"] is True
    assert "Особое мнение (советник 3): a3: раунд 2" in decision["rationale"]
    assert len(decision["rationale"]) < 600


def test_second_round_can_change_minds(make):
    script = make_script({"a1": [1], "a2": [2], "a3": [3]}, round2_votes={"a1": [2], "a2": [2], "a3": [2]})
    project = make(script, stage=True)
    run = run_auto(project, sphere(project))
    decision = next(c for c in project.store.cards.values() if c["kind"] == "роль")["decision"]
    assert run.status == "finished" and decision["chosen"]["indexes"] == [2] and decision["disputed"]
    assert "Особое мнение" not in decision["rationale"]


def test_no_consensus_first_adviser_decides_and_says_so(make):
    script = make_script({"a1": [1], "a2": [2], "a3": [3]})
    project = make(script, stage=True)
    run_auto(project, sphere(project))
    decision = next(c for c in project.store.cards.values() if c["kind"] == "роль")["decision"]
    assert decision["chosen"]["indexes"] == [1] and decision["rationale"].startswith("Согласия не было, решил советник 1.")


def test_council_that_cannot_decide_falls_back_to_human(make):
    script = make_script({"a1": [1], "a2": None, "a3": None})
    project = make(script, stage=True)
    run = run_auto(project, sphere(project))
    assert run.status == "asking" and run.question["question"] == "Какие роли?"
    assert any("Совет не смог решить" in m["text"] for m in project.chat)
    project.answer("st", run.question["id"], {"type": "choice", "indexes": [2]})
    run = wait_for(lambda: project.runs["st"], ("finished",), timeout=15)
    card = next(c for c in project.store.cards.values() if c["kind"] == "роль")
    assert card["decision"]["by"] == HUMAN


def test_council_question_limit(make, monkeypatch):
    from idea_graph_engine import runtime
    monkeypatch.setattr(runtime, "AUTO_MAX_QUESTIONS", 1)
    results = []

    def script(note, call, request):
        if request.model.startswith("a"):
            call("decide", choice="choice", options=[1], rationale="ок")
            return ""
        if "Ответ на твой вопрос" in note:
            results.append(call("ask", question="Ещё?", options=["а", "б"]))
            call("finish", outcome="готово", summary="ок")
            return ""
        if "ПРЕДЛОЖИ-РОЛИ" in note:
            call("ask", question="Первый?", options=["а", "б"])
            return ""
        call("finish", outcome="готово", summary="ок")
        return ""

    project = make(script, stage=True)
    run_auto(project, sphere(project))
    assert results and results[0].startswith("ОТКАЗ auto_limit")
