"""Ворота шагов этапа и устаревание: что утверждено, что требует пересмотра.

Состояние лежит в ``.idea-graph/stages/<пакет>.json``:
``{"steps": {id: {"status": "open"|"approved", "ts": …}}, "stale": {id: {"reason", "rev", "cards": […]}}}``.
Утверждает шаг только человек. Если человек потом меняет карточку утверждённого шага, зависимые шаги
помечаются «требует пересмотра» (а помощник может предложить правки, см. ``Project._propagate``).
"""

from __future__ import annotations

import json
import os
import re
import time

from idea_graph_core import ACCEPTED, HUMAN, PROPOSED

from .errors import EngineError

STATUS_OPEN, STATUS_APPROVED = "open", "approved"


class StageError(EngineError):
    """Ошибка шага этапа (нет шага, нечего утверждать, есть непринятое)."""


def steps_of(manifest: dict) -> list[dict]:
    return list((manifest.get("stage") or {}).get("steps") or [])


def step_by_id(manifest: dict, step_id: str) -> dict:
    for step in steps_of(manifest):
        if step["id"] == step_id:
            return step
    raise StageError("unknown_step", f"В этапе «{manifest['name']}» нет шага {step_id}")


def step_of_kind(manifest: dict, kind: str) -> dict | None:
    return next((s for s in steps_of(manifest) if kind in s["produces"]), None)


def downstream(manifest: dict, step_id: str) -> list[str]:
    """Шаги, которые прямо или через другие зависят от шага (в порядке карты)."""
    steps = steps_of(manifest)
    reached = {step_id}
    changed = True
    while changed:
        changed = False
        for step in steps:
            if step["id"] not in reached and any(n in reached for n in step["needs"]):
                reached.add(step["id"])
                changed = True
    return [s["id"] for s in steps if s["id"] in reached and s["id"] != step_id]


class StageState:
    def __init__(self, project, package_id: str):
        self.path = project.dir / "stages" / f"{package_id}.json"
        self.project = project
        self.package = package_id
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        self.steps: dict = data.get("steps") or {}
        self.stale: dict = data.get("stale") or {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps({"steps": self.steps, "stale": self.stale}, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(tmp, self.path)

    def status(self, step_id: str) -> str:
        return (self.steps.get(step_id) or {}).get("status") or STATUS_OPEN


def _cards_of(store, kinds: list[str]) -> list[dict]:
    return [c for c in store.cards.values() if c["kind"] in kinds]


def field_value(card: dict, name: str) -> str:
    """Значение поля из описания карточки: последняя строка вида «Название: значение» (в нижнем регистре)."""
    found = re.findall(rf"(?im)^\s*{re.escape(name)}\s*:\s*(.+?)\s*$", card.get("body") or "")
    return found[-1].strip().rstrip(".").lower() if found else ""


def _requirement(project, step: dict) -> tuple[bool, int, int]:
    """Условие шага без собственных карточек (``requires``): (выполнено, сколько есть, сколько нужно)."""
    need = step["requires"]
    count = sum(1 for c in project.store.cards.values()
                if c["kind"] == need["kind"] and field_value(c, need["field"]) == str(need["value"]).lower())
    minimum = int(need.get("min", 1))
    return count >= minimum, count, minimum


def describe(project, manifest: dict) -> dict:
    """Состояние шагов для интерфейса: статус, устаревание, сколько карточек и записей банка."""
    state = StageState(project, manifest["id"])
    out = []
    for step in steps_of(manifest):
        cards = _cards_of(project.store, step["produces"])
        item = {
            "id": step["id"], "status": state.status(step["id"]),
            "ts": (state.steps.get(step["id"]) or {}).get("ts"),
            "stale": state.stale.get(step["id"]),
            "cards": len(cards), "proposed": sum(1 for c in cards if c["status"] == PROPOSED),
        }
        if step.get("requires"):
            ok, count, minimum = _requirement(project, step)
            item["required"] = {"ok": ok, "count": count, "min": minimum}
        bank_name = step.get("produces_bank")
        if bank_name:
            records = project.store.bank(bank_name).list()
            item["records"] = len(records)
            item["records_proposed"] = sum(1 for r in records if (r.get("data") or {}).get("state") == "proposed")
        out.append(item)
    return {"package": manifest["id"], "steps": out}


def approve(project, manifest: dict, step_id: str, accept_all: bool = False) -> dict:
    step = step_by_id(manifest, step_id)
    state = StageState(project, manifest["id"])
    cards = _cards_of(project.store, step["produces"])
    bank_name = step.get("produces_bank")
    records = project.store.bank(bank_name).list() if bank_name else []
    if step.get("requires"):
        ok, count, minimum = _requirement(project, step)
        if not ok:
            need = step["requires"]
            raise StageError("empty_step", f"Нужно отметить не меньше {minimum} (сейчас {count}): "
                                           f"{need['kind']} с полем «{need['field']}: {need['value']}»")
    elif not cards and not records:
        raise StageError("empty_step", "У шага ещё нет результата: нечего утверждать")
    proposed = [c for c in cards if c["status"] == PROPOSED]
    pending_records = [r for r in records if (r.get("data") or {}).get("state") == "proposed"]
    if (proposed or pending_records) and not accept_all:
        raise StageError("pending", f"Есть непринятые элементы: {len(proposed) + len(pending_records)}. "
                                    "Примите их или утвердите шаг с принятием всех")
    if proposed:
        project.human_ops([{"op": "update_card", "id": c["id"], "status": ACCEPTED} for c in proposed],
                          note=f"Шаг «{step['title']}» утверждён: принято предложенное")
    if pending_records:
        bank = project.store.bank(bank_name)
        for record in pending_records:
            data = dict(record["data"])
            data["state"] = "accepted"
            bank.update(record["id"], HUMAN, data=data)
    state.steps[step_id] = {"status": STATUS_APPROVED, "ts": time.time()}
    state.stale.pop(step_id, None)
    state.save()
    project._touch()
    return describe(project, manifest)


def reopen(project, manifest: dict, step_id: str) -> dict:
    step_by_id(manifest, step_id)
    state = StageState(project, manifest["id"])
    state.steps[step_id] = {"status": STATUS_OPEN, "ts": time.time()}
    state.save()
    project._touch()
    return describe(project, manifest)


def clear_stale(project, manifest: dict, step_id: str) -> dict:
    step_by_id(manifest, step_id)
    state = StageState(project, manifest["id"])
    state.stale.pop(step_id, None)
    state.save()
    project._touch()
    return describe(project, manifest)


def mark_changes(project, manifest: dict, changes: list[dict]) -> list[str]:
    """Человек изменил или удалил карточки: зависимые шаги утверждённых шагов — «требует пересмотра».

    ``changes`` — изменения графа (``Store.changes_since``). Возвращает шаги, получившие отметку.
    """
    state = StageState(project, manifest["id"])
    marked: list[str] = []
    for change in changes:
        if change["type"] != "card" or change["before"] is None:
            continue
        before, after = change["before"], change["after"]
        if after is not None and (before.get("title"), before.get("body")) == (after.get("title"), after.get("body")):
            continue                                  # сменился только статус или служебные поля
        step = step_of_kind(manifest, before["kind"])
        if step is None or state.status(step["id"]) != STATUS_APPROVED:
            continue
        what = "удалена" if after is None else "изменена"
        for target in downstream(manifest, step["id"]):
            entry = state.stale.setdefault(target, {"reason": "", "rev": 0, "cards": []})
            entry["rev"] = project.store.revision
            if before["id"] not in entry["cards"]:
                entry["cards"].append(before["id"])
            entry["reason"] = f"Карточка «{before['title']}» ({step['title']}) {what} после утверждения шага"
            if target not in marked:
                marked.append(target)
    if marked:
        state.save()
    return marked
