"""Настройки движка: какой агент и какая модель в каждой роли, лимиты, корневая папка проектов.

Лежат в домашней папке пользователя (``~/.idea-graph/settings.json``) и общие для всех проектов.
Сам граф проекта хранится в его папке, здесь только список недавних папок.

Роли агентов с моделями:
    ``default_slot`` и ``slots`` — основной агент пакета («помощник»);
    ``researchers`` — три исследователя (веб-поиск и ресурсы пользователя);
    ``advisers`` — три советника: совет принимает решения в автоматическом режиме;
    ``reviewer`` — независимое ревью.
"""

from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path

DEFAULTS = {
    "slots": {},                       # id пакета -> {"agent", "model", "effort"}
    "default_slot": {"agent": "claude", "model": "", "effort": ""},
    "researchers": [                   # три исследователя: им стоит дать разные модели
        {"agent": "claude", "model": "opus", "effort": ""},
        {"agent": "claude", "model": "sonnet", "effort": ""},
        {"agent": "claude", "model": "haiku", "effort": ""},
    ],
    "advisers": [                      # три советника автоматического режима
        {"agent": "claude", "model": "opus", "effort": ""},
        {"agent": "claude", "model": "sonnet", "effort": ""},
        {"agent": "claude", "model": "haiku", "effort": ""},
    ],
    "reviewer": {"agent": "claude", "model": "opus", "effort": ""},
    "auto_limits": {},                 # id пакета -> {ключ лимита: число} для автоматического режима
    "auto_unlimited": False,           # не ограничивать автоматический режим числом карточек
    "auto_research": True,             # в автоматическом режиме сначала исследовать область
    "auto_propagate": True,            # после правки утверждённого шага предлагать правки зависимого
    "projects_root": "",               # корневая папка проектов; пусто — «Документы\\Граф идеи»
    "recent": [],
}
_FLAGS = ("auto_research", "auto_unlimited", "auto_propagate")


def home() -> Path:
    return Path(os.environ.get("IDEA_GRAPH_HOME") or Path.home() / ".idea-graph")


def _merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def _slot(raw) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    return {"agent": str(raw.get("agent") or ""), "model": str(raw.get("model") or ""),
            "effort": str(raw.get("effort") or "")}


class Settings:
    def __init__(self, path: Path | None = None):
        self.path = path or home() / "settings.json"
        self.lock = threading.Lock()
        self.data = copy.deepcopy(DEFAULTS)
        self.load()

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        if isinstance(raw, dict):
            self.data = _merge(DEFAULTS, raw)

    def save(self) -> None:
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)

    def update(self, patch: dict) -> dict:
        patch = patch if isinstance(patch, dict) else {}
        simple = {k: v for k, v in patch.items() if k in ("slots", "default_slot")}
        self.data = _merge(self.data, simple)
        for flag in _FLAGS:
            if flag in patch:
                self.data[flag] = bool(patch[flag])
        if "projects_root" in patch:
            self.data["projects_root"] = str(patch["projects_root"] or "").strip()
        if isinstance(patch.get("auto_limits"), dict):
            clean: dict = {}
            for package, limits in patch["auto_limits"].items():
                if isinstance(limits, dict):
                    clean[str(package)] = {str(k): max(1, min(20, int(v))) for k, v in limits.items()
                                           if str(v).lstrip("-").isdigit()}
            self.data["auto_limits"] = clean
        for key in ("researchers", "advisers"):                  # ровно три слота
            if isinstance(patch.get(key), list) and len(patch[key]) == 3:
                self.data[key] = [_slot(item) for item in patch[key]]
        if isinstance(patch.get("reviewer"), dict):
            self.data["reviewer"] = _slot(patch["reviewer"])
        self.save()
        return self.data

    def slot(self, package_id: str) -> dict:
        slot = self.data["slots"].get(package_id) or {}
        base = self.data["default_slot"]
        return {"agent": slot.get("agent") or base.get("agent") or "claude",
                "model": slot.get("model", base.get("model", "")) or "",
                "effort": slot.get("effort", base.get("effort", "")) or ""}

    def auto_limits(self, manifest: dict) -> dict:
        """Лимиты автоматического режима пакета: значения из настроек поверх умолчаний пакета."""
        saved = (self.data.get("auto_limits") or {}).get(manifest["id"]) or {}
        spec = ((manifest.get("stage") or {}).get("auto") or {}).get("limits") or []
        return {item["key"]: int(saved.get(item["key"], item.get("default", 3))) for item in spec}

    def remember(self, folder: str) -> None:
        recent = [f for f in self.data.get("recent", []) if f != folder]
        self.data["recent"] = [folder, *recent][:12]
        self.save()
