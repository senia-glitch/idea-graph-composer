"""Проверка согласованности графа перед выгрузкой.

Код находит то, что можно проверить точно (``findings``), и даёт агенту граф текстом (``graph_text``), чтобы он нашёл
смысловые разногласия. Документ строится только по графу, поэтому правится граф, а не документ.
"""

from __future__ import annotations

import hashlib
import json

from idea_graph_core import ACCEPTED

from . import diagrams, flow

FIXES = ("remove", "choose_variant", "suggest_variants", "edit", "rebuild")
IMPORTANCE = ("низкая", "средняя", "высокая", "критическая")


def ref(card: dict) -> str:
    return f"«{card['title']}» ({card['id']})"


def basis(project, scenario_id: str, kind: str) -> str:
    """Отпечаток того, по чему построена диаграмма: принятые проблемы и (для решения) выбранные варианты."""
    problems = [[p["title"], p["body"]] for p in project._accepted(flow.PROBLEM, scenario_id)]
    solved = [[s["problem"], s["title"], s["body"]] for s in project._solved(scenario_id)] if kind == flow.SOLUTION else []
    return hashlib.sha1(json.dumps([problems, solved], ensure_ascii=False).encode("utf-8")).hexdigest()[:12]


def _pains(card: dict) -> set[str]:
    steps = ((card.get("attrs") or {}).get("diagram") or {}).get("steps") or []
    return {str(s["pain"].get("what", "")).strip().lower() for s in steps if isinstance(s.get("pain"), dict) and s["pain"].get("what")}


def findings(project) -> list[str]:
    """Разногласия, которые видны без чтения смысла: пустое, несвязанное, устаревшее."""
    out: list[str] = []
    used: set[str] = set()
    for scenario in project._accepted_all(flow.SCENARIO):
        sid = scenario["id"]
        roles = project.roles_of(sid)
        used |= {r["id"] for r in roles}
        if not roles:
            out.append(f"Сценарий {ref(scenario)}: не указано ни одной роли")
        if not scenario.get("body"):
            out.append(f"Сценарий {ref(scenario)}: нет описания")
        problems = project._accepted(flow.PROBLEM, sid)
        for problem in problems:
            if not problem.get("body"):
                out.append(f"Проблема {ref(problem)} (сценарий «{scenario['title']}»): нет описания")
            if (problem.get("attrs") or {}).get("importance") not in IMPORTANCE:
                out.append(f"Проблема {ref(problem)} (сценарий «{scenario['title']}»): не указана важность")
            chosen = project._accepted(flow.VARIANT, problem["id"])
            if not chosen:
                proposed = [c for c in project.cards(flow.VARIANT, problem["id"])]
                note = ("; есть предложенные варианты: " + ", ".join(ref(c) for c in proposed)) if proposed else "; вариантов решения нет"
                out.append(f"Проблема {ref(problem)} (сценарий «{scenario['title']}»): принята, а решение не выбрано{note}")
            for variant in chosen:
                if not variant.get("body"):
                    out.append(f"Решение {ref(variant)} проблемы «{problem['title']}»: нет описания")
        asis = project.cards(flow.ASIS, sid)
        if problems and not asis:
            out.append(f"Сценарий {ref(scenario)}: есть принятые проблемы, но нет диаграммы «как есть»")
        if asis:
            have, want = _pains(asis[0]), {p["title"].strip().lower() for p in problems}
            if have != want:
                out.append(f"Сценарий {ref(scenario)}: диаграмма «как есть» не соответствует принятым проблемам "
                           "(на ней нет или есть лишние проблемы) — нужно перестроить")
        solution = project.cards(flow.SOLUTION, sid)
        if project._solved(sid) and not solution:
            out.append(f"Сценарий {ref(scenario)}: есть выбранные решения, но нет диаграммы «как будет»")
        if not project._solved(sid) and solution:
            out.append(f"Сценарий {ref(scenario)}: диаграмма «как будет» есть, а выбранных решений нет — она устарела")
        for kind, cards_ in ((flow.ASIS, asis), (flow.SOLUTION, solution)):
            for card in cards_[:1]:
                saved = (card.get("attrs") or {}).get("basis")
                if saved and saved != basis(project, sid, kind):
                    out.append(f"Сценарий {ref(scenario)}: диаграмма «{'как есть' if kind == flow.ASIS else 'как будет'}» построена "
                               "по старым проблемам или решениям — нужно перестроить")
    for role in project._accepted_all(flow.ROLE):
        if not role.get("body"):
            out.append(f"Роль {ref(role)}: нет описания")
        if role["id"] not in used:
            out.append(f"Роль {ref(role)}: не участвует ни в одном принятом сценарии")
    return out


def graph_text(project) -> str:
    """Граф проекта для чтения агентом: что принято, с id карточек."""
    out = ["Роли:"]
    roles = project._accepted_all(flow.ROLE)
    out += [f"- {ref(r)}: {r.get('body') or 'нет описания'}" for r in roles] or ["- нет"]
    for scenario in project._accepted_all(flow.SCENARIO):
        sid = scenario["id"]
        out += ["", f"Сценарий {ref(scenario)}: {scenario.get('body') or 'нет описания'}",
                "  роли: " + (", ".join(r["title"] for r in project.roles_of(sid)) or "нет")]
        cases = ((scenario.get("attrs") or {}).get("usecase") or {}).get("cases") or []
        out += [f"  вариант использования: {c.get('name')} ({', '.join(c.get('actors') or [])})" for c in cases]
        for problem in project._accepted(flow.PROBLEM, sid):
            chosen = project._accepted(flow.VARIANT, problem["id"])
            out.append(f"  проблема {ref(problem)}, важность {(problem.get('attrs') or {}).get('importance') or 'не указана'}: "
                       f"{problem.get('body') or 'нет описания'}")
            out += [f"    РЕШЕНИЕ {ref(v)}: {v.get('body') or 'нет описания'}" for v in chosen] or ["    решение не выбрано"]
            out += [f"    предложенный вариант {ref(v)}: {v.get('body')}" for v in project.cards(flow.VARIANT, problem["id"])
                    if v["status"] != ACCEPTED]
        for kind, label in ((flow.ASIS, "КАК ЕСТЬ"), (flow.SOLUTION, "КАК БУДЕТ")):
            for card in project.cards(kind, sid)[:1]:
                diagram = (card.get("attrs") or {}).get("diagram")
                out.append(f"  {label}: {card.get('body') or ''}")
                out += ["    " + line for line in diagrams.to_text(diagram).splitlines()] if diagram else ["    шагов нет"]
    return "\n".join(out)


def validate_fix(project, fix) -> dict:
    """Правка из варианта агента: известный вид, существующая карточка подходящего вида. Иначе ValueError."""
    if not isinstance(fix, dict) or fix.get("do") not in FIXES:
        raise ValueError("у каждого варианта должна быть правка fix.do: " + ", ".join(FIXES) + " (варианта «оставить» быть не может)")
    try:
        card = project.store.card(str(fix.get("id") or ""))
    except Exception:  # noqa: BLE001 — id придумал агент
        raise ValueError(f"нет карточки с id «{fix.get('id')}»: бери id только из графа") from None
    do, kind = fix["do"], card["kind"]
    allowed = {"remove": (flow.ROLE, flow.SCENARIO, flow.PROBLEM, flow.VARIANT),
               "choose_variant": (flow.VARIANT,), "suggest_variants": (flow.PROBLEM,),
               "edit": (flow.ROLE, flow.SCENARIO, flow.PROBLEM, flow.VARIANT), "rebuild": (flow.SCENARIO,)}[do]
    if kind not in allowed:
        raise ValueError(f"правка {do} не подходит к карточке {ref(card)} (вид «{kind}»)")
    clean = {"do": do, "id": card["id"]}
    if do == "edit":
        for field in ("title", "body"):
            if str(fix.get(field) or "").strip():
                clean[field] = " ".join(str(fix[field]).split()) if field == "title" else str(fix[field]).strip()
        if str(fix.get("importance") or "").strip().lower() in IMPORTANCE and kind == flow.PROBLEM:
            clean["importance"] = str(fix["importance"]).strip().lower()
        if len(clean) == 2:
            raise ValueError("в правке edit нужен хотя бы один из title, body, importance")
    return clean
