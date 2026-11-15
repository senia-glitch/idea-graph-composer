"""Совет из трёх агентов: принимает решение вместо человека в автоматическом режиме.

Как работает:
1. Каждый советник (свой слот модели в настройках) независимо выносит решение с коротким обоснованием.
2. Если мнения разошлись — один круг обсуждения: каждый видит позиции остальных и решает окончательно.
3. Итог — решение большинства (одинаковый выбор у двоих и более); если полного совпадения нет, берутся
   варианты, за которые высказались минимум двое; если и таких нет — решает первый советник, об этом
   сказано в обосновании.
Обоснование итога — 2–3 фразы от советника из победившей группы плюс «особое мнение» несогласных одной
строкой. Пересказа переписки в карточку не попадает.
"""

from __future__ import annotations

import secrets
import threading
import types

from idea_graph_core import views

from .tools import ADVISER_RULES

MAX_RATIONALE = 300
MAX_DISSENT = 140
OUTLINE_LIMIT = 5000


def brief(project, run, question: dict, positions: list[dict] | None = None) -> str:
    manifest = project.registry.get(run.package) or {"name": run.package}
    lines = [f"Вопрос агента этапа «{manifest['name']}»: {question['question']}"]
    if question.get("comment"):
        lines.append("Комментарий агента: " + question["comment"])
    lines.append("Варианты:")
    for number, option in enumerate(question["options"], 1):
        lines.append(f"{number}. {option['title']}" + (f" — {option['consequence']}" if option.get("consequence") else ""))
    if question.get("recommended"):
        lines.append("Агент рекомендует: " + ", ".join(str(i) for i in question["recommended"])
                     + (f" ({question['reason']})" if question.get("reason") else ""))
    lines.append("Можно выбрать любое число вариантов, ни одного (rejected) или выбрать и дописать своё (text).")
    outline = views.outline(project.store)
    lines.append("Граф проекта:\n" + (outline if len(outline) <= OUTLINE_LIMIT else outline[:OUTLINE_LIMIT] + "\n…"))
    if positions:
        lines.append("Позиции советников в первом круге (теперь реши окончательно, можно изменить позицию):")
        for position in positions:
            lines.append(f"- Советник {position['n']}: {position['value']}. Обоснование: {position['rationale']}")
    return "\n\n".join(lines)


def _signature(chosen: dict) -> tuple:
    if chosen["type"] == "rejected":
        return ("rejected",)
    return ("choice", tuple(chosen.get("indexes") or ()), " ".join((chosen.get("text") or "").lower().split()))


def _ask(project, run, question: dict, note: str, positions: list[dict] | None) -> list[dict | None]:
    """Один круг: все советники отвечают параллельно. Возвращает позиции в порядке слотов (None — не ответил)."""
    from .runtime import describe_choice

    slots = project.settings.data["advisers"]
    results: list[dict | None] = [None] * len(slots)

    def work(index: int) -> None:
        slot = slots[index]
        token = secrets.token_urlsafe(24)
        holder = types.SimpleNamespace(proc=None)
        box = {"kind": "adviser", "question": question, "decision": None, "note": note}
        with project.lock:
            project.tokens[token] = box
            run.research_holders.append(holder)
        try:
            outcome = project._slot_request(slot, project.dir / "agent" / f"adviser-{index + 1}",
                                            ADVISER_RULES, note, token, "", False, holder)
        finally:
            with project.lock:
                project.tokens.pop(token, None)
        with project.lock:
            run.cost += outcome.cost
        if run.stop_flag:
            return
        if outcome.error or box["decision"] is None:
            project.say("system", f"Советник {index + 1}: {outcome.error or 'не вынес решения'}", run.package, "error")
            return
        decision = box["decision"]
        results[index] = {"n": index + 1, "chosen": decision["chosen"], "rationale": decision["rationale"],
                          "value": describe_choice(decision["chosen"])}

    threads = [threading.Thread(target=work, args=(i,), daemon=True, name=f"adviser-{i + 1}") for i in range(len(slots))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results


def tally(positions: list[dict]) -> tuple[dict, list[dict], bool]:
    """Итог по позициям: (выбор, победившая группа, есть ли согласие). Позиции — только ответившие."""
    groups: dict[tuple, list[dict]] = {}
    for position in positions:
        groups.setdefault(_signature(position["chosen"]), []).append(position)
    best = max(groups.values(), key=len)
    if len(best) >= 2:
        return best[0]["chosen"], best, True
    votes: dict[int, list[dict]] = {}
    for position in positions:
        for index in position["chosen"].get("indexes") or []:
            votes.setdefault(index, []).append(position)
    agreed = sorted(i for i, who in votes.items() if len(who) >= 2)
    if agreed:
        voters = [p for i in agreed for p in votes[i]]
        group = list({id(p): p for p in voters}.values())
        titles = [position["chosen"]["values"][position["chosen"]["indexes"].index(i)] for i in agreed
                  for position in votes[i][:1]]
        chosen = {"type": "choice", "indexes": agreed, "values": titles, "text": "", "value": "; ".join(titles)}
        return chosen, group, True
    return positions[0]["chosen"], [positions[0]], False


def convene(project, run, question: dict) -> dict | None:
    """Созвать совет по вопросу. None — совет не смог решить (меньше двух ответивших)."""
    positions = [p for p in _ask(project, run, question, brief(project, run, question), None) if p]
    if run.stop_flag or len(positions) < 2:
        return None
    disputed = False
    if len({_signature(p["chosen"]) for p in positions}) > 1:
        disputed = True
        second = [p for p in _ask(project, run, question, brief(project, run, question, positions), positions) if p]
        if run.stop_flag:
            return None
        if len(second) >= 2:
            positions = second
    chosen, winners, agreed = tally(positions)
    first = winners[0]
    rationale = first["rationale"][:MAX_RATIONALE]
    if not agreed:
        rationale = ("Согласия не было, решил советник 1. " + rationale)[:MAX_RATIONALE + 40]
    dissent = [p for p in positions if p not in winners]
    for position in dissent[:2]:
        rationale += f" Особое мнение (советник {position['n']}): {position['rationale'][:MAX_DISSENT]}"
    return {"chosen": chosen, "by": "council", "rationale": rationale.strip(), "disputed": disputed,
            "assumption": False,
            "positions": [{"n": p["n"], "value": p["value"], "rationale": p["rationale"][:MAX_RATIONALE]}
                          for p in positions]}
