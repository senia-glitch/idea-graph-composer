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
        self.skipped: bool = bool(data.get("skipped"))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps({"steps": self.steps, "stale": self.stale, "skipped": self.skipped},
                                  ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def entry(self, step_id: str) -> dict:
        return self.steps.setdefault(step_id, {"status": STATUS_OPEN})

    def runs(self, step_id: str) -> int:
        return int((self.steps.get(step_id) or {}).get("runs") or 0)

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


def _link_parent(store, card_id: str) -> dict | None:
    for edge in store.edges.values():
        if edge["kind"] == "детализирует" and edge["from"] == card_id:
            return store.cards.get(edge["to"])
    return None


def issues(project, manifest: dict, step: dict, state: StageState) -> list[dict]:
    """Открытые вопросы шага: что ещё можно улучшить (непринятое, пробелы, замечания, устаревшее, правки)."""
    store = project.store
    out: list[dict] = []
    cards = _cards_of(store, step["produces"])
    proposed = [c for c in cards if c["status"] == PROPOSED]
    if proposed:
        out.append({"type": "pending", "text": f"Не принято карточек: {len(proposed)}"})
    bank_name = step.get("produces_bank")
    if bank_name:
        waiting = [r for r in store.bank(bank_name).list() if (r.get("data") or {}).get("state") == "proposed"]
        if waiting:
            out.append({"type": "pending", "text": f"Не принято записей банка: {len(waiting)}"})
    stale = state.stale.get(step["id"])
    if stale:
        out.append({"type": "stale", "text": stale["reason"]})
    for card in sorted(_cards_of(store, ["замечание"]), key=lambda c: int(c["id"][1:])):
        parent = _link_parent(store, card["id"])
        belongs = (card.get("attrs") or {}).get("step") == step["id"] or (
            parent is not None and parent["kind"] in step["produces"] and not (card.get("attrs") or {}).get("step"))
        if belongs:
            out.append({"type": "remark", "text": card["title"], "card": card["id"]})
    for card in sorted(_cards_of(store, ["правка"]), key=lambda c: int(c["id"][1:])):
        target = store.cards.get(str((card.get("attrs") or {}).get("target")))
        if target is not None and target["kind"] in step["produces"]:
            out.append({"type": "proposal", "text": f"Предложена правка к «{target['title']}»", "card": card["id"]})
    check = step.get("checks")
    if callable(check):
        from .runtime import PackageContext
        try:
            for text in check(PackageContext(project, manifest["id"]), step) or []:
                out.append({"type": "check", "text": str(text)})
        except Exception as error:  # noqa: BLE001 — сломанная проверка пакета не должна ронять интерфейс
            out.append({"type": "check", "text": f"Проверка шага не сработала: {error}"})
    return out


def describe(project, manifest: dict) -> dict:
    """Состояние шагов для интерфейса: статус, число прогонов, открытые вопросы, сколько карточек и записей банка."""
    state = StageState(project, manifest["id"])
    out = []
    for step in steps_of(manifest):
        cards = _cards_of(project.store, step["produces"])
        item = {
            "id": step["id"], "status": state.status(step["id"]),
            "ts": (state.steps.get(step["id"]) or {}).get("ts"),
            "stale": state.stale.get(step["id"]), "runs": state.runs(step["id"]),
            "cards": len(cards), "proposed": sum(1 for c in cards if c["status"] == PROPOSED),
            "issues": issues(project, manifest, step, state),
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
    return {"package": manifest["id"], "skipped": state.skipped, "steps": out}


def bump_runs(project, manifest: dict, step_id: str) -> None:
    """Ещё один прогон шага (любое действие помощника): показывается в карте, чтобы было видно, сколько раз шаг улучшали."""
    state = StageState(project, manifest["id"])
    entry = state.entry(step_id)
    entry["runs"] = int(entry.get("runs") or 0) + 1
    state.save()


def set_skipped(project, manifest: dict, flag: bool) -> dict:
    """Часть этапа можно пропустить целиком (например, «Разведку»): пропущенное не считается незавершённым."""
    state = StageState(project, manifest["id"])
    state.skipped = bool(flag)
    state.save()
    project._touch()
    return describe(project, manifest)


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
    entry = state.entry(step_id)
    entry.update({"status": STATUS_APPROVED, "ts": time.time()})
    state.stale.pop(step_id, None)
    state.save()
    project._touch()
    return describe(project, manifest)


def reopen(project, manifest: dict, step_id: str) -> dict:
    step_by_id(manifest, step_id)
    state = StageState(project, manifest["id"])
    state.entry(step_id).update({"status": STATUS_OPEN, "ts": time.time()})
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


def _started(project, manifest: dict, state: StageState, step_id: str) -> bool:
    """Есть ли у шага результат (карточки, записи банка) или он уже утверждён."""
    step = step_by_id(manifest, step_id)
    if state.status(step_id) == STATUS_APPROVED or _cards_of(project.store, step["produces"]):
        return True
    return bool(step.get("produces_bank") and project.store.bank(step["produces_bank"]).list())


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
            if not _started(project, manifest, state, target):
                continue                                  # зависимый шаг ещё пуст: пересматривать нечего
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
