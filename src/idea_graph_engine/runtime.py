"""Открытый проект: граф, работа пакетов-этапов, вопросы и ответы, чат.

Главный цикл работы пакета (:meth:`Project._work`):

1. Если есть неотвеченный вопрос — ждём человека; в автоматическом режиме на вопрос отвечает совет
   из трёх агентов (:mod:`council`).
2. Если пакет вызвал ``finish`` — подвести итог (или перейти к следующему шагу автоматического режима).
3. Иначе собрать вводные хода и запустить агента на один ход.

Помощник шага (:meth:`Project.start` с ``step`` и ``action``) — это тот же цикл с вводными шага:
предложить, проверить (ревьюер на своём слоте модели), уточнить, сгенерировать, предложить правки зависимого.
"""

from __future__ import annotations

import copy
import json
import os
import secrets
import threading
import time
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from idea_graph_core import HUMAN, GraphError, Store, author_name, views

from . import council, resources, stage
from .agents import Adapters, TurnRequest, TurnResult
from .config import Settings
from .errors import EngineError
from .packages import Registry
from .tools import (COMMON_RULES, ADVISER_TOOLS, PACKAGE_TOOLS, PROPOSER_RULES, RESEARCHER_RULES, RESEARCH_TOOLS,
                    REVIEWER_RULES, REVIEW_TOOLS)

BUSY = ("running", "deciding", "researching")
WAITING = ("asking",)
CHAT_KEEP = 400
AUTO_MAX_TURNS = 4          # сколько раз автоматический режим продолжит работу, если агент не вызвал finish
AUTO_MAX_QUESTIONS = 12     # сколько вопросов за запуск автоматический режим отдаст совету
ACTIONS = {"suggest": "предложить", "review": "независимое ревью", "clarify": "уточнить и проверить согласованность",
           "generate": "сгенерировать", "propagate": "предложить правки зависимого"}
ROLE_KINDS = {"reviewer": ("замечание",), "proposer": ("правка",)}


class PackageContext:
    """То, что получает код пакета (``prepare``, ``conclude``, подсказки шагов)."""

    def __init__(self, project: "Project", package_id: str):
        self.store = project.store
        self.folder = project.folder
        self.author = f"package:{package_id}"
        self.registry = project.registry
        self.bank = project.store.bank
        self._path = project.dir / "packages" / f"{package_id}.json"
        try:
            self.state = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.state = {}
        self.host = project

    def save_state(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self.state, ensure_ascii=False, indent=1), encoding="utf-8")


class Run:
    """Состояние работы одного пакета над этим проектом."""

    SAVED = ("package", "status", "session_id", "turn", "last_read", "question", "inbox", "outcome", "summary",
             "error", "cost", "last_answer", "finished", "autorun", "autorun_turns", "role", "step", "action",
             "questions_asked")

    def __init__(self, package: str):
        self.package = package
        self.status = "idle"
        self.session_id = ""
        self.turn = 0
        self.last_read = 0
        self.question: dict | None = None
        self.inbox: list[str] = []
        self.outcome = ""
        self.summary = ""
        self.error = ""
        self.cost = 0.0
        self.last_answer: dict | None = None
        self.finished = False
        self.autorun: dict | None = None   # {"targets": [id], "queue": [[цель, шаг], …], "pos": 0, "research": bool}
        self.autorun_turns = 0
        self.role = ""                     # "" — помощник; "reviewer" — ревью; "proposer" — правки зависимого
        self.step = ""
        self.action = ""
        self.questions_asked = 0
        # не сохраняется:
        self.thread: threading.Thread | None = None
        self.proc = None
        self.stop_flag = False
        self.research_holders: list = []    # запущенные исследователи и советники: их можно остановить
        self.asked_this_turn = False
        self.note = ""

    def dump(self) -> dict:
        return {key: getattr(self, key) for key in self.SAVED}

    @classmethod
    def load(cls, data: dict) -> "Run":
        run = cls(str(data.get("package")))
        for key in cls.SAVED:
            if key in data:
                setattr(run, key, data[key])
        if run.status in BUSY:            # движок закрыли посреди хода
            run.status = "asking" if run.question else "idle"
        return run

    def public(self) -> dict:
        return {
            "package": self.package, "status": self.status, "turn": self.turn,
            "cost": round(self.cost, 4), "question": self.question,
            "outcome": self.outcome, "summary": self.summary, "error": self.error,
            "has_session": bool(self.session_id), "auto": self.autorun, "role": self.role,
            "step": self.step, "action": self.action,
        }


def describe_choice(chosen: dict) -> str:
    """Ответ словами: для чата и для вводных агента."""
    if chosen["type"] == "rejected":
        return "все варианты отклонены" + (f" ({chosen['value']})" if chosen.get("value") else "")
    parts = []
    if chosen["values"]:
        label = "выбран вариант" if len(chosen["values"]) == 1 else "выбраны варианты"
        parts.append(f"{label} {', '.join(str(i) for i in chosen['indexes'])} — "
                     + "; ".join(f"«{v}»" for v in chosen["values"]))
    if chosen.get("text"):
        parts.append(("и своё дополнение: " if chosen["values"] else "свой ответ: ") + f"«{chosen['text']}»")
    return "; ".join(parts)


class Project:
    PROPAGATE_DELAY = 15.0     # сколько ждать после правки человека, прежде чем предлагать правки зависимого

    def __init__(self, folder: str | os.PathLike, settings: Settings, registry: Registry,
                 adapters: Adapters | None = None):
        self.folder = Path(folder).resolve()
        self.store = Store.open(self.folder)
        self.dir = self.store.dir
        self.settings = settings
        self.registry = registry
        self.adapters = adapters or Adapters()
        self.base_url = ""
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.seq = 0                         # растёт при любом изменении состояния — для опроса из UI
        self.tokens: dict[str, dict] = {}
        self.runs: dict[str, Run] = {}
        self.chat: list[dict] = []
        self._timers: dict[str, threading.Timer] = {}
        self._load()

    # ------------------------------------------------------------------ хранение

    def _load(self) -> None:
        for path in sorted((self.dir / "runs").glob("*.json")) if (self.dir / "runs").is_dir() else []:
            try:
                run = Run.load(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
            self.runs[run.package] = run
        try:
            lines = (self.dir / "chat.jsonl").read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        for line in lines[-CHAT_KEEP:]:
            try:
                self.chat.append(json.loads(line))
            except ValueError:
                continue
        self.seq = max([m.get("seq", 0) for m in self.chat] + [0])

    def _touch(self, run: Run | None = None) -> None:
        with self.lock:
            self.seq += 1
            if run is not None:
                path = self.dir / "runs" / f"{run.package}.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(run.dump(), ensure_ascii=False, indent=1), encoding="utf-8")
            self.changed.notify_all()

    def say(self, who: str, text: str, package: str = "", kind: str = "message", **extra) -> None:
        with self.lock:
            self.seq += 1
            entry = {"seq": self.seq, "ts": time.time(), "who": who, "package": package,
                     "kind": kind, "text": text, **extra}
            self.chat.append(entry)
            del self.chat[:-CHAT_KEEP]
            with open(self.dir / "chat.jsonl", "a", encoding="utf-8") as stream:
                stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self.changed.notify_all()

    # ------------------------------------------------------------------ состояние для UI

    def state(self, chat_after: int = 0) -> dict:
        with self.lock:
            return {
                "folder": str(self.folder),
                "title": self.store.title or self.folder.name,
                "revision": self.store.revision,
                "seq": self.seq,
                "runs": {key: run.public() for key, run in self.runs.items()},
                "stages": {m["id"]: stage.describe(self, m) for m in self.registry.stages()},
                "chat": [m for m in self.chat if m["seq"] > chat_after],
            }

    # ------------------------------------------------------------------ запуск и остановка

    def _manifest(self, package_id: str) -> dict:
        manifest = self.registry.get(package_id)
        if manifest is None:
            raise EngineError("unknown_package", f"Пакет «{package_id}» не установлен")
        return manifest

    def _stage_manifest(self, package_id: str) -> dict:
        manifest = self._manifest(package_id)
        if not isinstance(manifest.get("stage"), dict):
            raise EngineError("no_stage", f"У пакета «{manifest['name']}» нет карты этапа")
        return manifest

    def _card(self, card_id: str) -> dict:
        card = self.store.cards.get(card_id)
        if card is None:
            raise EngineError("unknown_card", f"Карточки {card_id} нет в графе")
        return card

    def start(self, package_id: str, fresh: bool = False, auto: dict | None = None, step: str | None = None,
              action: str | None = None, target: str | None = None, goal: str = "", phase: str | None = None) -> dict:
        """Запустить пакет, помощника шага или автоматический режим.

        * без ``step``/``auto`` — продолжить диалог пакета с человеком;
        * ``step`` и ``action`` — помощник шага (``suggest``, ``review``, ``clarify``, ``generate``);
          ``action="propagate"`` — предложить правки зависимого после изменения утверждённого шага;
        * ``auto={"targets": [id, …]}`` — автоматический режим: вопросов человеку нет, на развилках решает совет;
          цели — карточки верхнего уровня этапа (пусто — все). Нужна хотя бы одна.

        ``goal`` — необязательное «что улучшить» от человека; шаг с готовым результатом улучшается, а не делается заново.
        ``phase`` — вместо шага: проверить (``review``) или уточнить (``clarify``) сразу весь крупный этап.
        """
        manifest = self._manifest(package_id)
        if manifest["kind"] != "agent":
            raise EngineError("not_runnable", f"Пакет «{manifest['name']}» не запускается как шаг")
        step_def, role, phase_steps = None, "", []
        if phase and not step and action in ("review", "clarify"):
            self._stage_manifest(package_id)
            phase_steps = stage.phase_steps(manifest, phase, action)
            if not phase_steps:
                raise EngineError("no_action", f"У этапа «{phase}» нет действия «{ACTIONS[action]}»")
            step_def = phase_steps[0]
            role = "reviewer" if action == "review" else ""
        elif step or action:
            self._stage_manifest(package_id)
            action = action or "suggest"
            if action not in ACTIONS:
                raise EngineError("no_action", "Действие: " + ", ".join(ACTIONS))
            if action == "propagate":
                role = "proposer"
            else:
                if not step:
                    raise EngineError("invalid_input", "Укажите шаг этапа")
                step_def = stage.step_by_id(manifest, step)
                if not step_def["prompts"].get(action):
                    raise EngineError("no_action", f"У шага «{step_def['title']}» нет действия «{ACTIONS[action]}»")
                role = "reviewer" if action == "review" else ""
        target_card = self._card(target) if target else None
        auto_info = None
        if auto is not None:
            auto_info = self._auto_plan(manifest, auto)
            fresh = True
        with self.lock:
            run = self.runs.get(package_id)
            if run is not None and run.status in BUSY:
                raise EngineError("busy", f"Пакет «{manifest['name']}» уже работает")
            if run is not None and run.question is not None and (role or step_def or auto_info):
                raise EngineError("open_question", "Сначала ответьте на вопрос агента или остановите его")
            if run is None or fresh or auto_info or run.role != role:
                old = run
                run = Run(package_id)
                if old is not None:
                    run.cost = old.cost
                self.runs[package_id] = run
            run.autorun = auto_info
            run.autorun_turns = 0
            run.questions_asked = 0
            run.role = role
            run.step = step_def["id"] if step_def else ""
            run.action = action or ""
            run.stop_flag = False
            run.finished = False
            run.error = run.outcome = run.summary = ""
            if auto_info:
                run.inbox.append("Начало работы пакета в автоматическом режиме.")
            elif action == "propagate":
                run.inbox.append(self._propagate_text(manifest))
            elif phase_steps:
                run.inbox.append(self._phase_text(manifest, phase, phase_steps, action, target_card))
            elif step_def:
                run.inbox.append(self._action_text(manifest, step_def, action, target_card, str(goal or "").strip()[:2000]))
            elif run.question is None and not run.inbox:
                run.inbox.append("Начало работы пакета." if run.turn == 0 else "Человек попросил продолжить работу.")
            run.status = "running"
            self._touch(run)
        if step_def:
            stage.bump_runs(self, manifest, step_def["id"])
        label = (" (автоматически)" if auto_info else f" — {ACTIONS[action]}" if action else "")
        self.say("system", f"Запущен пакет «{manifest['name']}»{label}"
                 + (f", этап «{phase}»" if phase_steps else f", шаг «{step_def['title']}»" if step_def else ""), package_id, "start")
        self._spawn(run, self._work, prepare=True, target=target)
        return run.public()

    def _auto_plan(self, manifest: dict, auto: dict) -> dict:
        spec = (manifest.get("stage") or {}).get("auto")
        if manifest["kind"] != "agent" or not isinstance(spec, dict):
            raise EngineError("no_auto", f"У пакета «{manifest['name']}» нет автоматического режима")
        wanted = [str(x) for x in (auto.get("targets") or [])]
        pool = sorted((c for c in self.store.cards.values() if c["kind"] == spec["targets"]),
                      key=lambda c: int(c["id"][1:]))
        targets = [c for c in pool if not wanted or c["id"] in wanted]
        if wanted and len(targets) != len(set(wanted)):
            raise EngineError("unknown_target", f"Выбрана карточка, которой нет среди «{spec['targets']}»")
        if not targets:
            raise EngineError("no_target", f"Автоматический режим требует хотя бы одну карточку «{spec['targets']}»: "
                                           "напишите её вручную")
        steps = [s["id"] for s in stage.steps_of(manifest) if s["prompts"].get("suggest")]
        research = bool(self.settings.data.get("auto_research", True)) if auto.get("research") is None else bool(auto["research"])
        return {"targets": [c["id"] for c in targets], "research": research, "pos": 0,
                "queue": [[c["id"], s] for c in targets for s in steps]}

    def _improve_text(self, manifest: dict, step: dict, goal: str) -> str:
        """Вводные «улучшения»: что уже есть у шага, что открыто и что просит человек. Пусто, если шаг ещё не начат и цели нет."""
        state = stage.StageState(self, manifest["id"])
        cards = [c for c in self.store.cards.values() if c["kind"] in step["produces"]]
        records = self.store.bank(step["produces_bank"]).list() if step.get("produces_bank") else []
        lines = []
        if goal:
            lines.append(f"ЧТО УЛУЧШИТЬ (задача человека): {goal}")
        if cards or records:
            lines.append("У шага уже есть результат, поэтому это УЛУЧШЕНИЕ, а не создание заново: прочитай существующее (graph_read, "
                         "bank_read), не дублируй, добавляй недостающее и согласуй с соседними шагами. Карточки человека и принятые "
                         "карточки не правь и не удаляй: предлагай карточку «правка» (attrs: target — id карточки, field — title или "
                         "body, new — новое значение целиком, reason — короткая причина). Свои непринятые карточки переписывать можно.")
        found = stage.issues(self, manifest, step, state)
        if found and (cards or records):
            lines.append("Открытые вопросы шага: " + "; ".join(i["text"] for i in found[:12]) + ".")
        return "\n".join(lines)

    def _action_text(self, manifest: dict, step: dict, action: str, target: dict | None, goal: str = "") -> str:
        prompt = step["prompts"][action]
        ctx = PackageContext(self, manifest["id"])
        text = prompt(ctx, step, target) if callable(prompt) else str(prompt)
        head = [f"Шаг этапа «{step['title']}»: {step['question']}".strip(),
                f"Действие: {ACTIONS[action]}."]
        if target:
            head.append(f"Цель: карточка {target['id']} «{target['title']}».")
        if action != "review":
            improve = self._improve_text(manifest, step, goal)
            if improve:
                head.append(improve)
        return "\n".join(head) + "\n" + text

    def _phase_text(self, manifest: dict, phase: str, steps: list[dict], action: str, target: dict | None) -> str:
        """Вводные для проверки или уточнения сразу всего крупного этапа: подсказки всех его шагов подряд."""
        ctx = PackageContext(self, manifest["id"])
        parts = [f"Крупный этап «{phase}». Действие: {ACTIONS[action]}."]
        if target:
            parts.append(f"Цель: карточка {target['id']} «{target['title']}».")
        for step in steps:
            prompt = step["prompts"][action]
            parts.append(f"— Шаг «{step['title']}»: " + (prompt(ctx, step, target) if callable(prompt) else str(prompt)))
        return "\n".join(parts)

    def _propagate_text(self, manifest: dict) -> str:
        state = stage.StageState(self, manifest["id"])
        lines = ["Человек изменил утверждённые карточки, и зависимые шаги могли устареть. Предложи правки зависимого."]
        for step_id, entry in state.stale.items():
            title = stage.step_by_id(manifest, step_id)["title"]
            lines.append(f"- шаг «{title}»: {entry['reason']}; изменённые карточки: " + ", ".join(entry["cards"]))
        lines.append("Прочитай граф (graph_read) и проверь карточки этих шагов. "
                     "Для каждой, которая теперь противоречит изменению, создай карточку-правку.")
        return "\n".join(lines)

    def _spawn(self, run: Run, function, **kwargs) -> None:
        thread = threading.Thread(target=self._guard, args=(run, function), kwargs=kwargs, daemon=True,
                                  name=f"run-{run.package}")
        run.thread = thread
        thread.start()

    def _guard(self, run: Run, function, **kwargs) -> None:
        try:
            function(run, **kwargs)
        except EngineError as error:
            self._fail(run, error.message)
        except Exception as error:  # noqa: BLE001 — ошибка пакета не должна ронять движок
            self._fail(run, f"Внутренняя ошибка: {error}")

    def _kick(self, run: Run) -> None:
        with self.lock:
            if run.thread is not None and run.thread.is_alive():
                return
            run.stop_flag = False
            run.status = "running"
            run.error = ""
            self._touch(run)
            self._spawn(run, self._work)

    def stop(self, package_id: str) -> dict:
        with self.lock:
            run = self.runs.get(package_id)
            if run is None:
                raise EngineError("not_started", "Пакет не запускался")
            run.stop_flag = True
            proc = run.proc
            if run.status not in BUSY:
                run.status = "stopped"
                self._touch(run)
        for holder in [types.SimpleNamespace(proc=proc), *list(run.research_holders)]:
            if getattr(holder, "proc", None) is not None:
                try:
                    holder.proc.kill()
                except OSError:
                    pass
        return run.public()

    def wait(self, package_id: str, timeout: float | None = None) -> dict:
        """Дождаться, пока пакет закончит (успехом, ошибкой или остановкой)."""
        deadline = None if timeout is None else time.time() + timeout
        with self.changed:
            while True:
                run = self.runs.get(package_id)
                if run is None or run.status in ("finished", "error", "stopped", "idle"):
                    return run.public() if run else {"status": "idle"}
                left = None if deadline is None else deadline - time.time()
                if left is not None and left <= 0:
                    return run.public()
                self.changed.wait(left if left is not None else 5)

    def _fail(self, run: Run, message: str) -> None:
        with self.lock:
            run.status = "error"
            run.error = message
            run.proc = None
            self._touch(run)
        self.say("system", message, run.package, "error")

    # ------------------------------------------------------------------ главный цикл пакета

    def _work(self, run: Run, prepare: bool = False, target: str | None = None) -> None:
        manifest = self._manifest(run.package)
        if prepare:
            if callable(manifest.get("prepare")):
                text = manifest["prepare"](PackageContext(self, run.package))
                if text:
                    with self.lock:
                        run.inbox.append(str(text))
            if run.autorun:
                if not self._auto_enter(run, manifest):
                    return
            elif run.step and run.action == "suggest":
                step = stage.step_by_id(manifest, run.step)
                if step.get("research") and (manifest["stage"].get("research") or {}).get("tasks"):
                    text = self._research(run, manifest, step, self.store.cards.get(target or ""))
                    if text is None:
                        return
                    if text:
                        with self.lock:
                            run.inbox.append(text)
        retried = False
        while True:
            with self.lock:
                if run.stop_flag:
                    run.status = "stopped"
                    run.proc = None
                    self._touch(run)
                    return
                question = run.question
            if question is not None:
                if not run.autorun:
                    with self.lock:
                        run.status = "asking"
                        self._touch(run)
                    return
                with self.lock:
                    run.status = "deciding"
                    self._touch(run)
                answer = self._council_answer(run, question)
                if run.stop_flag:
                    continue
                if answer is None:
                    with self.lock:
                        run.status = "asking"
                        self._touch(run)
                    return
                self._deliver(run, question, answer)
                continue
            if run.finished and not run.inbox:
                if run.autorun and self._auto_advance(run, manifest):
                    continue
                self._conclude(run, manifest)
                return
            with self.lock:
                if not run.inbox:
                    run.status = "idle"
                    self._touch(run)
                    return
                run.status = "running"
                run.finished = False
                events = list(run.inbox)
                note = self._note(run, manifest)
                self._touch(run)
            result = self._turn(run, manifest, note)
            if run.stop_flag or result.error:
                with self.lock:                      # ход не состоялся: его вводные не должны пропасть
                    run.inbox = events + run.inbox
                    self._touch(run)
            if run.stop_flag:
                continue
            if result.error:
                lost = any(mark in result.error.lower() for mark in ("no conversation found", "session not found", "session id"))
                if run.session_id and lost and not retried:
                    retried = True
                    with self.lock:
                        run.session_id = ""
                        run.inbox.insert(0, "Прошлая сессия недоступна, это новая сессия: продолжай работу по графу.")
                        self._touch(run)
                    continue
                self._fail(run, f"Агент не смог выполнить ход: {result.error}")
                return
            retried = False
            with self.lock:
                run.session_id = result.session_id or run.session_id
                run.turn += 1
                run.cost += result.cost
                silent = run.question is None and not run.finished
                if run.role and not run.finished and run.question is None:
                    run.finished = True            # ревьюер и «правщик» работают одним ходом
                self._touch(run)
            if silent and result.text.strip():
                self.say("agent", result.text.strip(), run.package)
            if run.autorun and not run.finished and run.question is None and not run.stop_flag:
                with self.lock:
                    more = run.autorun_turns < AUTO_MAX_TURNS
                    if more:
                        run.autorun_turns += 1
                        run.inbox.append("Автоматический режим: работа ещё не закончена. Продолжай и заверши её через finish.")
                if not more:
                    self.say("system", "Автоматический режим остановлен: агент не завершил работу за "
                             f"{AUTO_MAX_TURNS + 1} хода. Запустите ещё раз, чтобы продолжить.", run.package, "pause")

    # ---- автоматический режим: очередь «цель × шаг»

    def _auto_enter(self, run: Run, manifest: dict) -> bool:
        """Подготовить текущий элемент очереди: вводные шага и (по настройке) исследование. False — остановка."""
        queue, pos = run.autorun["queue"], run.autorun["pos"]
        if pos >= len(queue):
            with self.lock:
                run.finished = True
                run.inbox.append("В этапе нет шагов с действием «предложить».")
            return True
        card_id, step_id = queue[pos]
        step = stage.step_by_id(manifest, step_id)
        target = self.store.cards.get(card_id)
        with self.lock:
            run.step, run.action = step_id, "suggest"
            stage.bump_runs(self, manifest, step_id)
            if pos == 0:
                spec = manifest["stage"]["auto"]
                targets = [self.store.cards[i] for i in run.autorun["targets"] if i in self.store.cards]
                limits = None if self.settings.data.get("auto_unlimited") else self.settings.auto_limits(manifest)
                run.inbox.append(spec["note"](PackageContext(self, run.package), targets, limits))
            run.inbox.append(self._action_text(manifest, step, "suggest", target))
        if run.autorun.get("research") and step.get("research") and (manifest["stage"].get("research") or {}).get("tasks"):
            text = self._research(run, manifest, step, target)
            if text is None:
                return False
            if text:
                with self.lock:
                    run.inbox.append(text)
        return True

    def _auto_advance(self, run: Run, manifest: dict) -> bool:
        with self.lock:
            run.autorun["pos"] += 1
            more = run.autorun["pos"] < len(run.autorun["queue"])
            if more:
                run.finished = False
                run.outcome = ""
                run.autorun_turns = 0
        if not more:
            return False
        self.say("system", "Переход к следующему шагу автоматического режима.", run.package, "step")
        return self._auto_enter(run, manifest) or False

    # ---- исследование: параллельные исследователи

    def _research(self, run: Run, manifest: dict, step: dict, target: dict | None) -> str | None:
        """Исследователи ищут в сети и в ресурсах пользователя; возвращает вводные для основного агента."""
        spec = manifest["stage"]["research"]
        materials, problems = resources.texts(self.dir)
        for message in problems:
            self.say("system", f"Ресурс не прочитан: {message}", run.package, "error")
        ctx = PackageContext(self, run.package)
        tasks = spec["tasks"](ctx, step, target, materials)
        if not tasks:
            return ""
        slots = self.settings.data["researchers"]
        mode = step.get("research_mode") or spec.get("mode") or "replicate"
        jobs = ([(task, i) for task in tasks for i in range(len(slots))] if mode == "replicate"
                else [(task, k % len(slots)) for k, task in enumerate(tasks)])
        with self.lock:
            run.status = "researching"
            run.research_holders = []
            self._touch(run)
        self.say("system", f"Исследование: задач {len(tasks)}, исследователей {len(slots)}"
                 + (f"; материалов пользователя: {len(materials)}" if materials else "; материалов пользователя нет, ищем в сети")
                 + ". Это может занять несколько минут.", run.package, "research")
        errors: list[str] = []
        reports: dict[int, list[dict]] = {}

        def work(job: tuple[dict, int]) -> None:
            task, index = job
            slot = slots[index]
            token = secrets.token_urlsafe(24)
            holder = types.SimpleNamespace(proc=None)
            box = {"kind": "researcher", "note": task["note"], "report": None}
            with self.lock:
                self.tokens[token] = box
                run.research_holders.append(holder)
            try:
                result = self._slot_request(
                    slot, self.dir / "agent" / f"researcher-{index + 1}",
                    spec["skill"].strip() + "\n\n" + RESEARCHER_RULES, task["note"], token, "", True, holder)
            finally:
                with self.lock:
                    self.tokens.pop(token, None)
            with self.lock:
                run.cost += result.cost
            if run.stop_flag:
                return
            if result.error or box["report"] is None:
                errors.append(f"«{task['title']}», исследователь {index + 1}: {result.error or 'не прислал отчёт'}")
                return
            box["report"]["who"] = f"исследователь {index + 1} ({slot.get('agent')}/{slot.get('model') or 'модель по умолчанию'})"
            with self.lock:
                reports.setdefault(id(task), []).append(box["report"])

        with ThreadPoolExecutor(max_workers=max(1, len(slots))) as pool:
            list(pool.map(work, jobs))
        if run.stop_flag:
            with self.lock:
                run.status = "stopped"
                self._touch(run)
            return None
        results = [{"task": task, "reports": reports.get(id(task), [])} for task in tasks]
        for message in errors:
            self.say("system", message, run.package, "error")
        if not any(item["reports"] for item in results):
            self._fail(run, "Исследователи не вернули результатов: " + "; ".join(errors))
            return None
        state = ctx.state.setdefault("research", {})
        state[step["id"]] = {"ts": time.time(), "tasks": [{"title": r["task"]["title"], "reports": r["reports"]} for r in results]}
        ctx.save_state()
        self.say("system", f"Исследование закончено: отчёты по {sum(1 for r in results if r['reports'])} из {len(tasks)} задач.",
                 run.package, "research")
        with self.lock:
            run.status = "running"
            self._touch(run)
        return spec["merge"](ctx, step, target, results)

    @staticmethod
    def _listarg(raw) -> list:
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                return []
        return raw if isinstance(raw, list) else []

    def _tool_report(self, ctx: dict, args: dict) -> str:
        if ctx["report"] is not None:
            raise EngineError("one_report", "Отчёт уже отправлен: заверши ход")

        def text(value, limit):
            return " ".join(str(value or "").split())[:limit]

        findings = []
        for item in self._listarg(args.get("findings"))[:60]:
            if not isinstance(item, dict) or not text(item.get("title"), 200):
                continue
            data = item.get("data")
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except ValueError:
                    data = {}
            data = data if isinstance(data, dict) else {}
            if len(json.dumps(data, ensure_ascii=False)) > 3000:
                data = {}
            findings.append({"kind": text(item.get("kind"), 40), "title": text(item.get("title"), 200),
                             "description": text(item.get("description"), 600), "data": data,
                             "sources": [text(s, 300) for s in self._listarg(item.get("sources")) if text(s, 300)][:5]})
        if not findings:
            raise EngineError("empty_report", "В отчёте нет находок. Найди хотя бы что-то подтверждённое")
        ctx["report"] = {"findings": findings, "summary": text(args.get("summary"), 600)}
        return "Принято. Заверши ход."

    # ---- запрос к агенту

    def _slot_request(self, slot: dict, workdir: Path, system_text: str, note: str, token: str,
                      session: str, web: bool, run) -> TurnResult:
        adapter = self.adapters.get(slot.get("agent") or "")
        if adapter is None:
            return TurnResult(error=f"Агент «{slot.get('agent')}» не настроен. Откройте «Настройки».")

        def remember(proc):
            if run is not None:
                run.proc = proc

        request = TurnRequest(
            workdir=workdir, system_text=system_text, note=note,
            env={"IDEA_GRAPH_URL": self.base_url, "IDEA_GRAPH_TOKEN": token},
            session_id=session, model=slot.get("model") or "", effort=slot.get("effort") or "",
            web_search=web, on_process=remember, extra={"token": token, "project": self},
        )
        try:
            return adapter.run(request)
        finally:
            if run is not None:
                run.proc = None

    def _turn(self, run: Run, manifest: dict, note: str) -> TurnResult:
        token = secrets.token_urlsafe(24)
        with self.lock:
            run.asked_this_turn = False
            run.note = note
            self.tokens[token] = {"kind": "package", "run": run, "manifest": manifest}
        slot = self.settings.data["reviewer"] if run.role == "reviewer" else self.settings.slot(run.package)
        system = manifest["skill"].strip() + "\n\n" + COMMON_RULES
        if run.role == "reviewer":
            system += "\n\n" + REVIEWER_RULES
        elif run.role == "proposer":
            system += "\n\n" + PROPOSER_RULES
        try:
            return self._slot_request(slot, self.dir / "agent" / (run.package + (f"-{run.role}" if run.role else "")),
                                      system, note, token, run.session_id, bool(manifest.get("web_search")), run)
        finally:
            with self.lock:
                self.tokens.pop(token, None)

    def _note(self, run: Run, manifest: dict) -> str:
        """Вводные хода: события, чужие изменения графа, а в первом ходе сессии — весь граф."""
        parts = [f"ХОД {run.turn + 1} пакета «{manifest['name']}»."]
        parts.extend(run.inbox)
        run.inbox = []
        if not run.session_id:
            parts.append("Граф целиком:\n" + views.outline(self.store))
        else:
            changes = views.changes_text(self.store, run.last_read, exclude_author=f"package:{run.package}")
            parts.append("Изменения графа с твоего прошлого хода, сделанные не тобой:\n" + changes
                         if changes else "Чужих изменений графа с твоего прошлого хода нет.")
        parts.append(f"Текущая ревизия графа: {self.store.revision}. Возможные итоги для finish: "
                     + ", ".join(manifest["outcomes"]) + ".")
        run.last_read = self.store.revision
        return "\n\n".join(parts)

    def _conclude(self, run: Run, manifest: dict) -> None:
        outcome = run.outcome or manifest["outcomes"][0]
        if callable(manifest.get("conclude")) and not run.role:
            outcome = manifest["conclude"](PackageContext(self, run.package), outcome) or outcome
        with self.lock:
            run.outcome = outcome
            run.status = "finished"
            self._touch(run)
        self.say("agent", run.summary or "Работа закончена.", run.package, "finish", outcome=outcome)

    # ------------------------------------------------------------------ совет и ответы

    def _council_answer(self, run: Run, question: dict) -> dict | None:
        try:
            answer = council.convene(self, run, question)
        except Exception as error:  # noqa: BLE001
            answer = None
            self.say("system", f"Совет не справился ({error}): на вопрос отвечаете вы.", run.package, "error")
        if answer is None and not run.stop_flag:
            self.say("system", "Совет не смог решить (нужны ответы минимум двух советников): на вопрос отвечаете вы.",
                     run.package, "pause")
        return answer

    @staticmethod
    def _clean_choice(question: dict, choice: dict) -> dict:
        """Ответ на вопрос: любое число вариантов (indexes) и/или своё (text) либо отказ от всех."""
        kind = choice.get("type")
        if kind == "rejected":
            return {"type": "rejected", "value": str(choice.get("value") or "").strip()[:2000]}
        if kind != "choice":
            raise EngineError("invalid_input", "Ответ: choice (варианты и/или своё) или rejected (отклонить всё)")
        raw = choice.get("indexes")
        raw = [] if raw is None else raw
        if not isinstance(raw, list):
            raise EngineError("invalid_input", "indexes — список номеров вариантов")
        indexes: list[int] = []
        for item in raw:
            try:
                index = int(item)
            except (TypeError, ValueError):
                raise EngineError("invalid_input", "Номер варианта должен быть числом") from None
            if not 1 <= index <= len(question["options"]):
                raise EngineError("invalid_input", "Такого варианта нет")
            if index not in indexes:
                indexes.append(index)
        indexes.sort()
        text = str(choice.get("text") or "").strip()[:4000]
        if not indexes and not text:
            raise EngineError("invalid_input", "Не выбран ни один вариант и нет своего текста")
        titles = [question["options"][i - 1]["title"] for i in indexes]
        return {"type": "choice", "indexes": indexes, "values": titles, "text": text,
                "value": "; ".join(titles + ([text] if text else []))}

    def _deliver(self, run: Run, question: dict, answer: dict) -> None:
        chosen = answer["chosen"]
        what = describe_choice(chosen)
        who = "человек" if answer["by"] == HUMAN else "совет"
        note = (f"Ответ на твой вопрос «{question['question']}»: {what}. Ответил {who}"
                + (" (после обсуждения советников)" if answer.get("disputed") else "") + "."
                + (f" Обоснование: {answer['rationale']}" if answer.get("rationale") else "")
                + ' Запиши результат в граф (у карточки-решения передай "decision":"last") и двигайся дальше.')
        decision = {
            "question": question["question"],
            "options": [o["title"] for o in question["options"]],
            "chosen": chosen, "by": answer["by"], "rationale": answer.get("rationale", ""),
            "assumption": bool(answer.get("assumption")), "disputed": bool(answer.get("disputed")),
        }
        if answer.get("positions"):
            decision["positions"] = answer["positions"]
        with self.lock:
            run.question = None
            run.last_answer = decision
            run.inbox.append(note)
            self._touch(run)
        self.say("human" if answer["by"] == HUMAN else "council", what[0].upper() + what[1:], run.package,
                 "answer", question=question["question"], rationale=answer.get("rationale", ""),
                 disputed=bool(answer.get("disputed")))

    # ------------------------------------------------------------------ действия человека

    def answer(self, package_id: str, question_id: str, choice: dict) -> dict:
        with self.lock:
            run = self.runs.get(package_id)
            if run is None:
                raise EngineError("not_started", "Пакет не запускался")
            question = run.question if run.question and run.question["id"] == question_id else None
            if question is None:
                raise EngineError("no_question", "Этот вопрос уже закрыт")
            if run.status in BUSY:
                raise EngineError("busy", "На этот вопрос сейчас отвечает совет")
            chosen = self._clean_choice(question, choice or {})
        self._deliver(run, question, {"chosen": chosen, "by": HUMAN})
        self._kick(run)
        return run.public()

    def message(self, package_id: str, text: str) -> dict:
        """Свободное сообщение человека пакету: станет вводной следующего хода."""
        text = str(text or "").strip()
        if not text:
            raise EngineError("invalid_input", "Пустое сообщение")
        self._manifest(package_id)
        with self.lock:
            run = self.runs.get(package_id)
            if run is None:
                run = self.runs[package_id] = Run(package_id)
            run.inbox.append("Сообщение человека: " + text[:6000])
            if run.question is not None and run.status not in BUSY:
                run.inbox.append("Твой вопрос «" + run.question["question"] + "» снят: человек написал сообщение "
                                 "вместо ответа. Учти его и, если нужно, задай вопрос заново.")
                run.question = None
            run.finished = False
            self._touch(run)
        self.say("human", text, package_id)
        self._kick(run)
        return run.public()

    def human_ops(self, ops: list[dict], note: str = "") -> dict:
        before = self.store.revision
        result = self.store.apply(ops, HUMAN, note=note)
        self._touch()
        self._after_human(before)
        return result

    def undo(self, revision: int) -> dict:
        result = self.store.undo(int(revision))
        self._touch()
        return result

    # ------------------------------------------------------------------ ворота, устаревание, правки зависимого

    def document(self) -> str:
        """Документ для разработки: собран из графа и банков; каждый пакет-этап пишет свои разделы (``manifest["document"]``)."""
        parts = [f"# {self.store.title or self.folder.name}", ""]
        packages = sorted(self.registry.items.values(), key=lambda m: (m.get("order") or 100, m["name"]))
        for manifest in packages:
            if callable(manifest.get("document")):
                text = str(manifest["document"](PackageContext(self, manifest["id"])) or "").strip()
                if text:
                    parts += [text, ""]
        if len(parts) <= 2:
            parts.append("Документ пока пуст: добавьте сферу и запустите помощника.")
        return "\n".join(parts).rstrip() + "\n"

    def stage_status(self, package_id: str) -> dict:
        return stage.describe(self, self._stage_manifest(package_id))

    def clear_stale(self, package_id: str, step_id: str) -> dict:
        return self._stage_call(stage.clear_stale, package_id, step_id)

    def _stage_call(self, function, package_id: str, *args) -> dict:
        try:
            return function(self, self._stage_manifest(package_id), *args)
        except stage.StageError as error:
            raise EngineError(error.code, error.message) from None

    def _after_human(self, before: int) -> None:
        """Человек изменил граф: отметить зависимые шаги и, если включено, предложить правки зависимого."""
        changes = [c for c in self.store.changes_since(before) if HUMAN in c.get("authors", [])]
        if not changes:
            return
        for manifest in self.registry.stages():
            if stage.mark_changes(self, manifest, changes) and self.settings.data.get("auto_propagate", True) \
                    and (manifest["stage"].get("propagate", True)):
                self._schedule_propagate(manifest["id"])
        self._touch()

    def _schedule_propagate(self, package_id: str) -> None:
        with self.lock:
            old = self._timers.pop(package_id, None)
            if old is not None:
                old.cancel()
            timer = threading.Timer(self.PROPAGATE_DELAY, self._run_propagate, args=(package_id,))
            timer.daemon = True
            self._timers[package_id] = timer
            timer.start()

    def _run_propagate(self, package_id: str) -> None:
        with self.lock:
            self._timers.pop(package_id, None)
            run = self.runs.get(package_id)
            if run is not None and (run.status in BUSY or run.question is not None):
                return                                  # занят: отметка «требует пересмотра» остаётся, запустит человек
        try:
            self.start(package_id, action="propagate")
        except EngineError:
            pass

    def propose_for_stale(self, package_id: str) -> dict:
        """Ручной запуск: предложить правки зависимого по отметкам «требует пересмотра»."""
        return self.start(package_id, action="propagate")

    def apply_proposal(self, card_id: str) -> dict:
        """Применить карточку-правку: поправить цель и убрать правку. Решение человека (кнопка)."""
        card = self._card(card_id)
        if card["kind"] != "правка":
            raise EngineError("not_proposal", "Это не карточка-правка")
        attrs = card.get("attrs") or {}
        field, new, target = attrs.get("field"), attrs.get("new"), attrs.get("target")
        if field not in ("title", "body") or not isinstance(new, str) or not new.strip():
            raise EngineError("bad_proposal", "В правке нет поля (title или body) или нового значения")
        if target not in self.store.cards:
            raise EngineError("unknown_card", "Карточки, которую правка должна изменить, больше нет")
        return self.human_ops([{"op": "update_card", "id": target, field: new.strip()},
                               {"op": "delete_card", "id": card_id}], note=f"Применена правка {card_id}")

    # ------------------------------------------------------------------ вызовы инструментов агентом

    def tools_for(self, token: str) -> list[dict]:
        ctx = self.tokens.get(token)
        if ctx is None:
            raise EngineError("bad_token", "Ход уже закончен: инструменты недоступны")
        if ctx["kind"] == "adviser":
            return ADVISER_TOOLS
        if ctx["kind"] == "researcher":
            return RESEARCH_TOOLS
        if ctx["run"].role:
            return REVIEW_TOOLS
        return PACKAGE_TOOLS

    def tool_call(self, token: str, tool: str, args: dict) -> str:
        ctx = self.tokens.get(token)
        if ctx is None:
            raise EngineError("bad_token", "Ход уже закончен: инструменты недоступны")
        args = args if isinstance(args, dict) else {}
        if ctx["kind"] == "researcher":
            if tool == "turn_info":
                return ctx["note"]
            if tool == "report":
                return self._tool_report(ctx, args)
            raise EngineError("unknown_tool", "У исследователя есть только turn_info и report")
        if tool == "graph_read":
            return self._tool_read(ctx, args)
        if ctx["kind"] == "adviser":
            if tool == "turn_info":
                return ctx["note"]
            if tool == "decide":
                return self._tool_decide(ctx, args)
            raise EngineError("unknown_tool", "У советника есть только turn_info, graph_read и decide")
        handler = {"turn_info": self._tool_turn, "graph_apply": self._tool_apply, "ask": self._tool_ask,
                   "finish": self._tool_finish, "bank_read": self._tool_bank_read,
                   "bank_write": self._tool_bank_write}.get(tool)
        if handler is None or (ctx["run"].role and tool == "ask"):
            raise EngineError("unknown_tool", f"Нет инструмента {tool}")
        return handler(ctx, args)

    def _tool_turn(self, ctx: dict, _args: dict) -> str:
        return ctx["run"].note

    def _tool_read(self, ctx: dict, args: dict) -> str:
        mode = args.get("mode") or "outline"
        run: Run | None = ctx.get("run") if ctx["kind"] == "package" else None
        if mode == "card":
            return views.card_view(self.store, str(args.get("id") or ""))
        if mode == "deleted":
            return views.deleted_text(self.store)
        if mode == "changes":
            if run is None:
                return views.outline(self.store)
            text = views.changes_text(self.store, run.last_read, exclude_author=f"package:{run.package}")
            run.last_read = self.store.revision
            return text or "Чужих изменений с твоего прошлого чтения нет."
        if mode == "outline":
            if run is not None:
                run.last_read = self.store.revision
            return views.outline(self.store)
        raise EngineError("invalid_input", "mode: outline, card, changes или deleted")

    def _tool_apply(self, ctx: dict, args: dict) -> str:
        run: Run = ctx["run"]
        ops = args.get("ops")
        if isinstance(ops, str):                 # некоторые модели присылают список строкой
            try:
                ops = json.loads(ops)
            except ValueError:
                raise EngineError("invalid_ops", "ops должен быть списком объектов") from None
        if not isinstance(ops, list):
            raise EngineError("invalid_ops", "ops должен быть списком объектов")
        ops = copy.deepcopy(ops)
        allowed = ROLE_KINDS.get(run.role)
        improving = (run.step or run.autorun) and not run.role
        for op in ops:
            if improving and isinstance(op, dict) and op.get("op") in ("update_card", "delete_card"):
                card = self.store.cards.get(str(op.get("id")))
                if card is not None and (card["author"] == HUMAN or card["status"] == "accepted"):
                    raise EngineError("improve_limit", f"Карточку {card['id']} правишь не ты: она создана человеком или принята. "
                                      "Предложи карточку «правка» (attrs: target, field, new, reason)")
            if run.role == "reviewer" and isinstance(op, dict) and op.get("op") == "add_card" and run.step:
                op["attrs"] = {**(op.get("attrs") or {}), "step": run.step}
            if allowed is not None:
                ok = isinstance(op, dict) and (op.get("op") == "add_edge" or (
                    op.get("op") == "add_card" and op.get("kind") in allowed))
                if not ok:
                    raise EngineError("role_limit", "Здесь можно только добавлять карточки вида «"
                                      + "», «".join(allowed) + "» (и связи к ним). Чужие карточки не правятся")
            if isinstance(op, dict) and op.get("decision") == "last":
                if run.last_answer is None:
                    raise EngineError("no_answer", 'Нет ответа, который можно записать как "decision":"last"')
                op["decision"] = copy.deepcopy(run.last_answer)
        result = self.store.apply(ops, f"package:{run.package}", base_revision=run.last_read,
                                  note=str(args.get("note") or ""))
        self._touch()
        created = ", ".join(f"{ref} → {card}" for ref, card in result.get("created", {}).items())
        ids = ", ".join(result.get("ids", []))
        return (f"Готово. Ревизия графа: {result['revision']}."
                + (f" Новые объекты: {ids}." if ids else "") + (f" Имена: {created}." if created else ""))

    def _tool_bank_read(self, ctx: dict, args: dict) -> str:
        run: Run = ctx["run"]
        manifest = ctx["manifest"]
        name = str(args.get("name") or "")
        banks = manifest["banks"]
        if name not in banks["reads"] and name not in banks["writes"]:
            raise EngineError("bank_denied", f"Пакет не читает банк «{name}»")
        records = self.store.bank(name).list(str(args["type"]) if args.get("type") else None)
        if args.get("key"):
            records = [r for r in records if r.get("key") == str(args["key"])]
        if not records:
            return f"В банке «{name}» записей нет."
        lines = [f"Банк «{name}»: записей {len(records)}."]
        for record in records[:80]:
            lines.append(f"[{record['id']}] {record['type']}"
                         + (f" ({record['key']})" if record.get("key") else "") + ": "
                         + json.dumps(record["data"], ensure_ascii=False)
                         + (" · источники: " + ", ".join(record["sources"]) if record["sources"] else ""))
        if len(records) > 80:
            lines.append(f"…и ещё {len(records) - 80}")
        return "\n".join(lines)

    def _tool_bank_write(self, ctx: dict, args: dict) -> str:
        run: Run = ctx["run"]
        manifest = ctx["manifest"]
        name = str(args.get("name") or "")
        if name not in manifest["banks"]["writes"]:
            raise EngineError("bank_denied", f"Пакет не пишет в банк «{name}»")
        records = self._listarg(args.get("records"))
        if not records:
            raise EngineError("invalid_records", "records — непустой список записей {type, data, sources, key}")
        bank = self.store.bank(name)
        done = []
        for item in records[:100]:
            if not isinstance(item, dict):
                continue
            data = item.get("data")
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except ValueError:
                    data = None
            if not isinstance(data, dict):
                raise EngineError("invalid_records", "data записи — объект")
            data = dict(data)
            data.setdefault("state", "proposed")          # написанное агентом ждёт решения человека
            record = bank.put(str(item.get("type") or ""), data, f"package:{run.package}",
                              sources=item.get("sources"), key=str(item.get("key") or ""))
            done.append(record["id"])
        self._touch()
        return f"Записано в банк «{name}»: {', '.join(done)}."

    def _tool_ask(self, ctx: dict, args: dict) -> str:
        run: Run = ctx["run"]
        if run.autorun and run.questions_asked >= AUTO_MAX_QUESTIONS:
            raise EngineError("auto_limit", "Лимит вопросов совету исчерпан: дальше реши сам и продолжай работу")
        if run.asked_this_turn or run.question is not None:
            raise EngineError("one_question", "За ход — один вопрос. Вопрос уже задан: заканчивай ход.")
        text = str(args.get("question") or "").strip()
        raw = args.get("options")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                raw = None
        if not text or not isinstance(raw, list) or not 2 <= len(raw) <= 4:
            raise EngineError("invalid_question", "Нужен текст вопроса и от 2 до 4 готовых вариантов решения")
        options = []
        for item in raw:
            if isinstance(item, str):
                item = {"title": item}
            title = str((item or {}).get("title") or "").strip()
            if not title:
                raise EngineError("invalid_question", "У каждого варианта должен быть title")
            options.append({"title": title[:300], "consequence": str(item.get("consequence") or "").strip()[:400]})
        known = self.store.cards

        def ids(key):
            value = args.get(key) or []
            return [c for c in value if isinstance(c, str) and c in known] if isinstance(value, list) else []

        raw_rec = args.get("recommended")
        if not isinstance(raw_rec, list):
            raw_rec = [] if raw_rec is None else [raw_rec]
        recommended: list[int] = []
        for item in raw_rec:
            try:
                number = int(item)
            except (TypeError, ValueError):
                continue
            if 1 <= number <= len(options) and number not in recommended:
                recommended.append(number)
        question = {
            "id": secrets.token_hex(6), "package": run.package, "question": text[:1000], "options": options,
            "recommended": recommended,
            "reason": str(args.get("reason") or "").strip()[:800],
            "comment": str(args.get("comment") or "").strip()[:1500],
            "cards": ids("cards"), "revisits": ids("revisits"), "asked_rev": self.store.revision,
            "state": "open",
        }
        with self.lock:
            run.asked_this_turn = True
            run.questions_asked += 1
            run.question = question
            self._touch(run)
        self.say("agent", text, run.package, "question", qid=question["id"], comment=question["comment"])
        return "Вопрос передан. Сейчас заверши ход: ответ придёт во вводных следующего хода."

    def _tool_finish(self, ctx: dict, args: dict) -> str:
        run: Run = ctx["run"]
        manifest = ctx["manifest"]
        outcome = str(args.get("outcome") or "").strip() or manifest["outcomes"][0]
        if outcome not in manifest["outcomes"]:
            raise EngineError("invalid_outcome", "Итог — один из: " + ", ".join(manifest["outcomes"]))
        if run.question is not None:
            raise EngineError("question_open", "Ты задал вопрос в этом ходе: дождись ответа, потом завершай работу")
        with self.lock:
            run.finished = True
            run.outcome = outcome
            run.summary = str(args.get("summary") or "").strip()[:3000]
            self._touch(run)
        return "Принято. Заверши ход."

    def _tool_decide(self, ctx: dict, args: dict) -> str:
        if ctx.get("decision") is not None:
            raise EngineError("already_decided", "Решение уже вынесено")
        choice = args.get("choice")
        question = ctx["question"]
        if choice == "choice":
            chosen = self._clean_choice(question, {"type": "choice", "indexes": args.get("options"),
                                                   "text": args.get("text")})
        elif choice == "rejected":
            chosen = {"type": "rejected", "value": ""}
        else:
            raise EngineError("invalid_input", "choice: choice или rejected")
        rationale = " ".join(str(args.get("rationale") or "").split())
        if not rationale:
            raise EngineError("invalid_input", "Нужно обоснование (rationale)")
        ctx["decision"] = {"chosen": chosen, "rationale": rationale[:council.MAX_RATIONALE]}
        return "Решение принято. Заверши ход."


__all__ = ["EngineError", "GraphError", "PackageContext", "Project", "Run", "author_name"]
