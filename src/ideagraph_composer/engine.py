"""Прогон по карте: чистый код, без модели.

Смотрит на итог законченного пакета и по стрелкам запускает следующий. Два режима:
``auto`` — идёт сам; ``step`` — после каждого шага ждёт, пока человек нажмёт «Дальше».
Состояние прогона лежит в папке идеи, карта — отдельно от идеи.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from .maps import ALWAYS


def choose(data: dict, node_id: str, outcome: str, loops: dict[str, int]) -> tuple[dict | None, str]:
    """Какая стрелка срабатывает после шага. Возвращает стрелку и причину остановки, если её нет."""
    edges = [e for e in data["edges"] if e["from"] == node_id]
    edge = next((e for e in edges if e["when"] == outcome), None) or next(
        (e for e in edges if e["when"] == ALWAYS), None)
    if edge is None:
        return None, "Карта пройдена до конца" if not edges else f"Для итога «{outcome}» стрелки нет"
    if loops.get(edge["id"], 0) >= edge["max_loops"]:
        return None, (f"Петля исчерпана: по стрелке «{edge['when']}» уже прошли {edge['max_loops']} раз(а). "
                      "Посмотрите, что мешает, и запустите снова")
    return edge, ""


class Runner:
    """Один прогон карты над одной идеей. ``host`` — открытая идея в оболочке."""

    def __init__(self, host):
        self.host = host
        self.path = Path(host.dir) / "packages" / "composer.json"
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.thread: threading.Thread | None = None
        self.state = {"status": "idle", "map": None, "mode": "auto", "current": "", "loops": {},
                      "history": [], "reason": ""}
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                self.state.update(saved)
                if self.state["status"] in ("running", "waiting_next"):
                    self.state.update(status="stopped", reason="Оболочку закрыли во время прогона")
        except (OSError, ValueError):
            pass

    def public(self) -> dict:
        with self.lock:
            return json.loads(json.dumps(self.state))

    def _set(self, **changes) -> None:
        with self.lock:
            self.state.update(changes)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.state, ensure_ascii=False, indent=1), encoding="utf-8")

    def start(self, data: dict, mode: str = "auto") -> dict:
        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                raise RuntimeError("Прогон уже идёт")
            self.wake.clear()
            self._set(status="running", map=data, mode="step" if mode == "step" else "auto",
                      current=data["start"], loops={}, history=[], reason="")
            self.thread = threading.Thread(target=self._loop, daemon=True, name="composer")
            self.thread.start()
        return self.public()

    def next(self) -> dict:
        """«Дальше» в пошаговом режиме."""
        self.wake.set()
        return self.public()

    def stop(self) -> dict:
        with self.lock:
            active = self.state["status"] in ("running", "waiting_next")
            current = self.state["current"]
            data = self.state["map"]
            if active:
                self._set(status="stopped", reason="Остановлено человеком")
        self.wake.set()
        if active and data:
            node = next((n for n in data["nodes"] if n["id"] == current), None)
            if node:
                try:
                    self.host.stop(node["package"])
                except Exception:  # noqa: BLE001 — пакет мог ещё не стартовать
                    pass
        return self.public()

    def _active(self) -> bool:
        return self.state["status"] in ("running", "waiting_next")

    def _loop(self) -> None:
        data = self.state["map"]
        nodes = {n["id"]: n for n in data["nodes"]}
        while self._active():
            node = nodes.get(self.state["current"])
            if node is None:
                self._set(status="stopped", reason="Шаг карты не найден")
                return
            try:
                self.host.start(node["package"], answerer=node["answerer"])
            except Exception as error:  # noqa: BLE001
                self._set(status="stopped", reason=f"Шаг не запустился: {getattr(error, 'message', error)}")
                return
            result = self.host.wait(node["package"])
            if not self._active():
                return
            status = result.get("status")
            if status != "finished":
                why = {"error": "Шаг закончился ошибкой: " + str(result.get("error") or ""),
                       "stopped": "Шаг остановлен",
                       "idle": "Агент закончил ход, не завершив работу пакета. Откройте «Граф» и продолжите его"}
                self._set(status="stopped", reason=why.get(status, f"Шаг остановился: {status}"))
                return
            outcome = result.get("outcome") or ""
            entry = {"node": node["id"], "package": node["package"], "outcome": outcome,
                     "summary": result.get("summary") or "", "ts": time.time()}
            edge, reason = choose(data, node["id"], outcome, self.state["loops"])
            with self.lock:
                history = self.state["history"] + [entry]
                if edge is None:
                    self._set(status="done" if reason.startswith("Карта пройдена") else "stopped",
                              reason=reason, history=history)
                    return
                loops = dict(self.state["loops"])
                loops[edge["id"]] = loops.get(edge["id"], 0) + 1
                self._set(history=history, loops=loops, current=edge["to"],
                          status="waiting_next" if self.state["mode"] == "step" else "running")
            if self.state["status"] == "waiting_next":
                self.wake.clear()
                while self.state["status"] == "waiting_next" and not self.wake.wait(0.5):
                    pass
                if self.state["status"] != "waiting_next":
                    return
                self._set(status="running")
