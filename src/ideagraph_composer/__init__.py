"""Пакет «Компоновщик»: карта пакетов и прогон по ней. Добавляет в оболочку экран «Конвейер»."""

from __future__ import annotations

import threading
from pathlib import Path

from . import maps
from .engine import Runner, choose

__version__ = "0.1.0"
__all__ = ["Runner", "choose", "manifest", "maps"]

_runners: dict[str, Runner] = {}
_lock = threading.Lock()


class ComposerError(Exception):
    """Ошибка для интерфейса: оболочка покажет её текст человеку."""

    def __init__(self, message: str):
        super().__init__(message)
        self.code = "composer"
        self.message = message

    def as_dict(self) -> dict:
        return {"error": self.code, "message": self.message}


def runner_for(project) -> Runner:
    key = str(project.folder)
    with _lock:
        runner = _runners.get(key)
        if runner is None or runner.host is not project:
            runner = _runners[key] = Runner(project)
        return runner


def _packages(app) -> dict[str, dict]:
    return app.registry.items


def api(app, method: str, rest: str, data: dict) -> dict:
    """Запросы экрана «Конвейер». ``app`` — оболочка: у неё есть пакеты и открытая идея."""
    packages = _packages(app)
    try:
        if rest == "maps":
            found = maps.load_all()
            if not found:
                found = [maps.save(maps.default_map(packages))]
            return {"maps": found,
                    "packages": [p for p in app.registry.public() if p["kind"] in maps.RUNNABLE],
                    "has_decider": any(m["kind"] == "decider" for m in packages.values())}
        if rest == "state":
            if app.project is None:
                return {"state": None, "runs": {}}
            return {"state": runner_for(app.project).public(),
                    "runs": {key: run.public() for key, run in app.project.runs.items()}}
        if method != "POST":
            raise ComposerError("Нет такого запроса")
        if rest == "save":
            saved = maps.save(data.get("map") or {})
            return {"map": saved, **maps.validate(saved, packages)}
        if rest == "validate":
            return maps.validate(maps.clean(data.get("map") or {}), packages)
        if rest == "delete":
            maps.delete(str(data.get("id") or ""))
            return {"ok": True}
        if rest == "default":
            return {"map": maps.default_map(packages)}
        project = app.need()
        runner = runner_for(project)
        if rest == "run":
            chosen = maps.save(data.get("map") or {})
            problems = maps.validate(chosen, packages)
            if problems["errors"]:
                raise ComposerError("Карту нельзя запустить: " + "; ".join(problems["errors"]))
            return {"state": runner.start(chosen, str(data.get("mode") or "auto"))}
        if rest == "next":
            return {"state": runner.next()}
        if rest == "stop":
            return {"state": runner.stop()}
    except (ValueError, RuntimeError) as error:
        raise ComposerError(str(error)) from None
    raise ComposerError("Нет такого запроса")


def manifest() -> dict:
    return {
        "id": "composer",
        "name": "Компоновщик",
        "description": "Карта пакетов: какой пакет за каким идёт и при каком итоге. Прогон по карте — "
                       "по шагам или автоматически.",
        "kind": "view",
        "tab": "Конвейер",
        "static": str(Path(__file__).parent / "static"),
        "api": api,
    }
