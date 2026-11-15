"""Как движок узнаёт об установленных пакетах.

Пакет ставится через pip в то же окружение и объявляет точку входа в группе
``idea_graph.packages``. Точка входа — функция без аргументов, которая возвращает манифест
(обычный словарь). Импортировать движок пакету не нужно.

Поля манифеста
--------------
``id``, ``name``, ``description`` — обязательные.
``kind``:
    ``agent`` — пакет-этап: движок запускает агента с текстом ``skill``;
    ``view``  — добавляет экран в оболочку (``tab``, ``static``, ``api``).
``skill`` — системная инструкция агента пакета (для ``agent``).
``order`` — место пакета в списке (меньше — раньше).
``model_level`` — рекомендуемый уровень модели: «сильная», «средняя», «лёгкая».
``outcomes`` — возможные итоги работы (``finish``).
``static`` — папка со статическими файлами пакета (отдаётся по ``/pkg/<id>/static/``).
``prepare(ctx) -> str`` — код перед сессией агента; возвращает вводные.
``conclude(ctx, outcome) -> str`` — код после завершения; может уточнить итог.
``banks`` — ``{"reads": [...], "writes": [...]}``: какие банки проекта пакет читает и пишет.

Этап (``stage``) — описание карты этапа, чтобы оболочка могла показать шаги, а движок — вести ворота:
    ``steps`` — список шагов ``{id, title, question, produces, needs, produces_bank, visual, bank, actions,
        prompts}``; ``prompts`` — тексты или функции ``(ctx, step, target) -> str`` для действий
        ``suggest`` (предложить), ``review`` (независимое ревью), ``clarify`` (уточнить),
        ``generate`` (сгенерировать), ``propagate`` (предложить правки зависимого);
    ``kinds`` — виды карточек ``{kind, title, color, fields, chips, renderer}``;
    ``hidden_edges`` — виды связей, которые не рисуются на холсте;
    ``roots`` — вид карточек верхнего уровня этапа («сфера»);
    ``auto`` — автоматический режим ``{targets, limits, note}``;
    ``research`` — исследование ``{skill, tasks, merge, mode}``.

``ctx`` — объект с полями ``store`` (граф), ``folder``, ``author``, ``state`` (словарь пакета,
сохраняется в папке проекта) и методом ``save_state()``.
"""

from __future__ import annotations

from importlib import metadata

GROUP = "idea_graph.packages"
KINDS = ("agent", "view")
PUBLIC = ("id", "name", "description", "kind", "model_level", "outcomes", "tab", "version", "order")


def normalize(raw: dict) -> dict:
    if not isinstance(raw, dict) or not raw.get("id") or not raw.get("name"):
        raise ValueError("манифест должен быть словарём с полями id и name")
    manifest = dict(raw)
    manifest["id"] = str(manifest["id"])
    manifest.setdefault("description", "")
    manifest.setdefault("kind", "agent")
    if manifest["kind"] not in KINDS:
        raise ValueError(f"неизвестный kind: {manifest['kind']}")
    manifest.setdefault("model_level", "средняя")
    manifest["outcomes"] = list(manifest.get("outcomes") or ["готово"])
    banks = manifest.get("banks") or {}
    manifest["banks"] = {"reads": list(banks.get("reads") or []), "writes": list(banks.get("writes") or [])}
    if manifest["kind"] == "agent" and not manifest.get("skill"):
        raise ValueError("у пакета-этапа должен быть текст skill")
    stage = manifest.get("stage")
    if stage is not None:
        if not isinstance(stage, dict) or not isinstance(stage.get("steps"), list) or not stage["steps"]:
            raise ValueError("stage должен быть словарём со списком steps")
        seen: set[str] = set()
        for step in stage["steps"]:
            if not isinstance(step, dict) or not step.get("id") or not step.get("title"):
                raise ValueError("у шага этапа должны быть id и title")
            if step["id"] in seen:
                raise ValueError(f"шаг {step['id']} повторяется")
            seen.add(step["id"])
            step.setdefault("question", "")
            step["produces"] = list(step.get("produces") or [])
            step["needs"] = list(step.get("needs") or [])
            step.setdefault("prompts", {})
        for step in stage["steps"]:
            for need in step["needs"]:
                if need not in seen:
                    raise ValueError(f"шаг {step['id']} зависит от неизвестного шага {need}")
        stage["kinds"] = list(stage.get("kinds") or [])
        stage["hidden_edges"] = list(stage.get("hidden_edges") or [])
    return manifest


def _public_stage(stage: dict) -> dict:
    steps = []
    for step in stage["steps"]:
        item = {k: step[k] for k in ("id", "title", "question", "help", "produces", "needs") if k in step}
        item["actions"] = [a for a in ("suggest", "review", "clarify", "generate") if step["prompts"].get(a)]
        for key in ("produces_bank", "visual", "bank", "target_kind", "requires"):
            if step.get(key):
                item[key] = step[key]
        item["research"] = bool(step.get("research"))
        steps.append(item)
    return {"steps": steps, "kinds": stage["kinds"], "hidden_edges": stage["hidden_edges"],
            "roots": stage.get("roots", ""), "propagate": bool(stage.get("propagate", True))}


class Registry:
    def __init__(self, discover: bool = True):
        self.items: dict[str, dict] = {}
        self.errors: list[str] = []
        if discover:
            self.discover()

    def discover(self) -> None:
        for point in metadata.entry_points(group=GROUP):
            try:
                manifest = normalize(point.load()())
                manifest.setdefault("version", point.dist.version if point.dist else "")
                self.items[manifest["id"]] = manifest
            except Exception as error:  # noqa: BLE001 — сломанный пакет не должен ронять оболочку
                self.errors.append(f"{point.name}: {error}")

    def register(self, manifest: dict) -> dict:
        manifest = normalize(manifest)
        self.items[manifest["id"]] = manifest
        return manifest

    def get(self, package_id: str) -> dict | None:
        return self.items.get(package_id)

    def stages(self) -> list[dict]:
        return [m for m in self.items.values() if isinstance(m.get("stage"), dict)]

    def public(self) -> list[dict]:
        ordered = sorted(self.items.values(), key=lambda m: (m.get("order") or 100, m["name"]))
        out = []
        for m in ordered:
            item = {key: m.get(key) for key in PUBLIC}
            item["banks"] = m["banks"]
            auto = m.get("auto") or (m.get("stage") or {}).get("auto")
            if isinstance(auto, dict):          # функции наружу не отдаём
                item["auto"] = {"targets": auto.get("targets"), "limits": list(auto.get("limits") or [])}
            item["research"] = isinstance(m.get("research"), dict) or isinstance((m.get("stage") or {}).get("research"), dict)
            if isinstance(m.get("stage"), dict):
                item["stage"] = _public_stage(m["stage"])
            out.append(item)
        return out
