"""Карта пакетов: хранение, проверка и карта по умолчанию.

Карта — шаблон, общий для всех идей: лежит в домашней папке (``~/.idea-graph/maps``).
Узел — установленный пакет, стрелка — «после этого запускать то» с условием по итогу пакета.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

ALWAYS = "*"                   # стрелка срабатывает при любом итоге
DEFAULT_LOOPS = 3              # сколько раз можно пройти по одной стрелке за прогон
RUNNABLE = ("agent", "code")
ROOT_KINDS = {"идея"}


def maps_dir() -> Path:
    return Path(os.environ.get("IDEA_GRAPH_HOME") or Path.home() / ".idea-graph") / "maps"


def clean(raw: dict) -> dict:
    """Привести карту к каноническому виду; лишние поля отбрасываются."""
    if not isinstance(raw, dict):
        raise ValueError("Карта должна быть объектом")
    map_id = re.sub(r"[^\w-]+", "-", str(raw.get("id") or "").strip()).strip("-")
    if not map_id:
        raise ValueError("У карты должен быть идентификатор")
    nodes, seen = [], set()
    for node in raw.get("nodes") or []:
        node_id = str(node.get("id") or "")
        if not node_id or node_id in seen or not node.get("package"):
            raise ValueError("У каждого узла должны быть свой id и пакет")
        seen.add(node_id)
        answerer = node.get("answerer") if node.get("answerer") in ("human", "decider") else ""
        nodes.append({"id": node_id, "package": str(node["package"]), "answerer": answerer,
                      "x": float(node.get("x") or 0), "y": float(node.get("y") or 0)})
    edges, seen_edges = [], set()
    for edge in raw.get("edges") or []:
        edge_id = str(edge.get("id") or "")
        if not edge_id or edge_id in seen_edges:
            raise ValueError("У каждой стрелки должен быть свой id")
        seen_edges.add(edge_id)
        if edge.get("from") not in seen or edge.get("to") not in seen:
            raise ValueError("Стрелка ведёт к несуществующему узлу")
        try:
            loops = max(1, min(50, int(edge.get("max_loops") or DEFAULT_LOOPS)))
        except (TypeError, ValueError):
            loops = DEFAULT_LOOPS
        edges.append({"id": edge_id, "from": edge["from"], "to": edge["to"],
                      "when": str(edge.get("when") or ALWAYS), "max_loops": loops})
    start = raw.get("start") if raw.get("start") in seen else (nodes[0]["id"] if nodes else "")
    return {"id": map_id, "name": str(raw.get("name") or map_id)[:120], "start": start,
            "nodes": nodes, "edges": edges}


def save(raw: dict) -> dict:
    data = clean(raw)
    folder = maps_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{data['id']}.json"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)
    return data


def load_all() -> list[dict]:
    out = []
    folder = maps_dir()
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        try:
            out.append(clean(json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, ValueError):
            continue
    return out


def load(map_id: str) -> dict | None:
    return next((m for m in load_all() if m["id"] == map_id), None)


def delete(map_id: str) -> None:
    path = maps_dir() / f"{map_id}.json"
    if path.is_file():
        path.unlink()


def default_map(packages: dict[str, dict]) -> dict:
    """Карта из того, что установлено: уточнение → проверка → (чисто) выгрузка, с петлёй назад."""
    order = [p for p in ("clarify", "research", "formalize", "check", "export", "handoff") if p in packages]
    # исследование и формализация стоят ниже: так стрелка-петля от проверки назад не ложится на карточки
    nodes = [{"id": f"n{i + 1}", "package": p, "answerer": "", "x": 60 + i * 360,
              "y": 280 if p in ("research", "formalize") else 80} for i, p in enumerate(order)]
    by_package = {n["package"]: n["id"] for n in nodes}
    edges = []

    def link(source, target, when=ALWAYS):
        edges.append({"id": f"a{len(edges) + 1}", "from": by_package[source], "to": by_package[target],
                      "when": when, "max_loops": DEFAULT_LOOPS})

    before_check = [p for p in order if p in ("clarify", "research", "formalize")]
    for first, second in zip(before_check, before_check[1:]):
        link(first, second, "готово")
    if "check" in by_package:
        if before_check:
            link(before_check[-1], "check", "готово")
            link("check", before_check[0], "есть_мешающие")
        after = [p for p in ("export", "handoff") if p in by_package]
        if after:
            link("check", after[0], "чисто")
        for first, second in zip(after, after[1:]):
            link(first, second, "готово")
    return clean({"id": "osnovnoy", "name": "От идеи до передачи в студию", "nodes": nodes, "edges": edges,
                  "start": nodes[0]["id"] if nodes else ""})


def validate(data: dict, packages: dict[str, dict]) -> dict:
    """Ошибки (с ними карту запускать нельзя) и предупреждения — до запуска."""
    errors, warnings = [], []
    nodes = {n["id"]: n for n in data["nodes"]}
    if not nodes:
        return {"errors": ["В карте нет ни одного пакета"], "warnings": []}
    name = lambda node_id: packages.get(nodes[node_id]["package"], {}).get("name") or nodes[node_id]["package"]

    for node in nodes.values():
        manifest = packages.get(node["package"])
        if manifest is None:
            errors.append(f"Пакет «{node['package']}» не установлен")
        elif manifest["kind"] not in RUNNABLE:
            errors.append(f"Пакет «{manifest['name']}» нельзя поставить шагом карты")
        elif node["answerer"] == "decider" and not any(m["kind"] == "decider" for m in packages.values()):
            errors.append(f"У шага «{manifest['name']}» отвечает решатель, но пакет-решатель не установлен")
    if data["start"] not in nodes:
        errors.append("Не выбран первый шаг")

    outgoing: dict[str, list[dict]] = {}
    for edge in data["edges"]:
        outgoing.setdefault(edge["from"], []).append(edge)
        manifest = packages.get(nodes[edge["from"]]["package"])
        if manifest and edge["when"] != ALWAYS and edge["when"] not in manifest["outcomes"]:
            errors.append(f"У пакета «{manifest['name']}» нет итога «{edge['when']}». Есть: "
                          + ", ".join(manifest["outcomes"]))
    for node_id, edges in outgoing.items():
        conditions = [e["when"] for e in edges]
        for condition in set(conditions):
            if conditions.count(condition) > 1:
                shown = "любом итоге" if condition == ALWAYS else f"итоге «{condition}»"
                errors.append(f"От шага «{name(node_id)}» при {shown} идут две стрелки: шаги выполняются по одному")
        manifest = packages.get(nodes[node_id]["package"])
        if manifest and ALWAYS not in conditions:
            missing = [o for o in manifest["outcomes"] if o not in conditions]
            if missing:
                warnings.append(f"После шага «{name(node_id)}» при итоге «{', '.join(missing)}» карта остановится")

    # достижимость и порядок: что создано раньше, чем понадобилось
    reach, order, todo = set(), [], [data["start"]] if data["start"] in nodes else []
    while todo:
        current = todo.pop(0)
        if current in reach:
            continue
        reach.add(current)
        order.append(current)
        todo.extend(e["to"] for e in outgoing.get(current, []))
    for node_id in nodes:
        if node_id not in reach and data["start"] in nodes:
            warnings.append(f"До шага «{name(node_id)}» нельзя дойти от первого шага")
    produced = set(ROOT_KINDS)
    everything = set(ROOT_KINDS)
    for node_id in order:
        everything.update((packages.get(nodes[node_id]["package"]) or {}).get("writes") or [])
    for node_id in order:
        manifest = packages.get(nodes[node_id]["package"])
        if not manifest:
            continue
        own = set(manifest.get("writes") or [])
        needs = [k for k in manifest.get("reads") or [] if k not in produced and k not in own and k not in everything]
        if needs:
            warnings.append(f"Шагу «{manifest['name']}» нужны карточки вида «{', '.join(needs)}», "
                            "но ни один шаг карты их не создаёт")
        produced.update(own)
    return {"errors": errors, "warnings": warnings}
