"""Шаги этапа как справочник действий помощника и устаревание зависимого.

Шаги нужны движку для трёх вещей: какие вводные дать помощнику, что у шага уже есть (карточки, записи банка, открытые вопросы)
и что считать зависимым, когда человек меняет принятую карточку. Утверждений, ворот и статусов шагов нет: состояние — это
сами карточки (предложена или принята).

Состояние лежит в ``.idea-graph/stages/<пакет>.json``:
``{"steps": {id: {"runs": N}}, "stale": {id: {"reason", "rev", "cards": […]}}}``.
"""

from __future__ import annotations

import json
import os

from idea_graph_core import PROPOSED

from .errors import EngineError


class StageError(EngineError):
    """Ошибка шага этапа (нет такого шага или этапа)."""


def steps_of(manifest: dict) -> list[dict]:
    return list((manifest.get("stage") or {}).get("steps") or [])


def step_by_id(manifest: dict, step_id: str) -> dict:
    for step in steps_of(manifest):
        if step["id"] == step_id:
            return step
    raise StageError("unknown_step", f"В этапе «{manifest['name']}» нет шага {step_id}")


def phase_steps(manifest: dict, phase: str, action: str) -> list[dict]:
    """Шаги крупного этапа («фазы») с действием ``action``."""
    return [s for s in steps_of(manifest) if s.get("phase") == phase and s["prompts"].get(action)]


def step_of_kind(manifest: dict, kind: str) -> dict | None:
    return next((s for s in steps_of(manifest) if kind in s["produces"]), None)


def downstream(manifest: dict, step_id: str) -> list[str]:
    """Шаги, которые прямо или через другие зависят от шага (в порядке этапа)."""
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
        tmp.write_text(json.dumps({"steps": self.steps, "stale": self.stale}, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def runs(self, step_id: str) -> int:
        return int((self.steps.get(step_id) or {}).get("runs") or 0)


def _cards_of(store, kinds: list[str]) -> list[dict]:
    return [c for c in store.cards.values() if c["kind"] in kinds]


def _link_parent(store, card_id: str) -> dict | None:
    for edge in store.edges.values():
        if edge["kind"] == "детализирует" and edge["from"] == card_id:
            return store.cards.get(edge["to"])
    return None


def issues(project, manifest: dict, step: dict, state: StageState) -> list[dict]:
    """Открытые вопросы шага: непринятое, пробелы по проверкам этапа, замечания, устаревшее, предложенные правки."""
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
    """Состояние шагов для интерфейса: сколько карточек и записей банка, число прогонов, открытые вопросы, устаревание."""
    state = StageState(project, manifest["id"])
    out = []
    for step in steps_of(manifest):
        cards = _cards_of(project.store, step["produces"])
        item = {
            "id": step["id"], "stale": state.stale.get(step["id"]), "runs": state.runs(step["id"]),
            "cards": len(cards), "proposed": sum(1 for c in cards if c["status"] == PROPOSED),
            "issues": issues(project, manifest, step, state),
        }
        bank_name = step.get("produces_bank")
        if bank_name:
            records = project.store.bank(bank_name).list()
            item["records"] = len(records)
            item["records_proposed"] = sum(1 for r in records if (r.get("data") or {}).get("state") == "proposed")
        out.append(item)
    return {"package": manifest["id"], "steps": out}


def bump_runs(project, manifest: dict, step_id: str) -> None:
    """Ещё один прогон шага (любое действие помощника)."""
    state = StageState(project, manifest["id"])
    entry = state.steps.setdefault(step_id, {})
    entry["runs"] = int(entry.get("runs") or 0) + 1
    state.save()


def clear_stale(project, manifest: dict, step_id: str) -> dict:
    step_by_id(manifest, step_id)
    state = StageState(project, manifest["id"])
    state.stale.pop(step_id, None)
    state.save()
    project._touch()
    return describe(project, manifest)


def _started(project, step: dict) -> bool:
    """Есть ли у шага результат (карточки, записи банка)."""
    if _cards_of(project.store, step["produces"]):
        return True
    return bool(step.get("produces_bank") and project.store.bank(step["produces_bank"]).list())


def mark_changes(project, manifest: dict, changes: list[dict]) -> list[str]:
    """Человек изменил или удалил принятую карточку: зависимые шаги, где уже что-то есть, помечаются «может устареть».

    ``changes`` — изменения графа (``Store.changes_since``). Предложенные карточки, которые человек ещё не принял, не в счёт.
    Возвращает шаги, получившие отметку.
    """
    state = StageState(project, manifest["id"])
    marked: list[str] = []
    for change in changes:
        if change["type"] != "card" or change["before"] is None:
            continue
        before, after = change["before"], change["after"]
        if before.get("status") != "accepted":
            continue
        if after is not None and (before.get("title"), before.get("body")) == (after.get("title"), after.get("body")):
            continue                                  # сменился только статус или служебные поля
        step = step_of_kind(manifest, before["kind"])
        if step is None:
            continue
        what = "удалена" if after is None else "изменена"
        for target in downstream(manifest, step["id"]):
            if not _started(project, step_by_id(manifest, target)):
                continue                              # зависимый шаг ещё пуст: пересматривать нечего
            entry = state.stale.setdefault(target, {"reason": "", "rev": 0, "cards": []})
            entry["rev"] = project.store.revision
            if before["id"] not in entry["cards"]:
                entry["cards"].append(before["id"])
            entry["reason"] = f"Карточка «{before['title']}» ({step['title']}) {what}: связанное могло устареть"
            if target not in marked:
                marked.append(target)
    if marked:
        state.save()
    return marked
