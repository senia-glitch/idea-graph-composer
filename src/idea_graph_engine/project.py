"""Проект: граф и банк знаний, действия человека, работа агентов.

Цепочка линейна (см. ``flow.py``). Человек принимает, отклоняет, удаляет и пишет своё; агенты только предлагают:
три исследователя ищут и анализируют конкурентов в сети, помощник предлагает роли, сценарии, проблемы,
варианты автоматизации и строит диаграммы. В спорной ситуации помощник задаёт вопрос с вариантами.
Одновременно идёт одна работа.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from pathlib import Path

from idea_graph_core import ACCEPTED, HUMAN, PROPOSED, GraphError, Store, normalize_url

from . import diagrams, flow, tools
from .agents import TurnRequest, TurnResult
from .errors import EngineError

AGENT = "agent"
RESEARCHERS = 3


def _clip(text, limit: int) -> str:
    return " ".join(str(text or "").split())[:limit]


class Run:
    """Одна работа агентов: что делается, на каком этапе, что спросили."""

    def __init__(self, kind: str, title: str):
        self.kind, self.title = kind, title
        self.status = "running"            # running | asking | done | error | stopped
        self.error = ""
        self.summary = ""
        self.question: dict | None = None
        self.stop = False
        self.procs: set = set()
        self.answer: dict | None = None
        self.answered = threading.Event()
        self.done = self.total = 0
        self.notes: list[str] = []         # что не вышло или пропущено: показываем человеку

    def public(self) -> dict:
        return {"kind": self.kind, "title": self.title, "status": self.status, "error": self.error,
                "summary": self.summary, "question": self.question, "done": self.done, "total": self.total,
                "notes": self.notes[-6:]}


class Project:
    def __init__(self, folder, settings, adapters):
        self.folder = Path(folder)
        self.store = Store.open(self.folder)
        self.dir = self.store.dir
        self.settings, self.adapters = settings, adapters
        self.bank = self.store.bank(flow.BANK)
        self.lock = threading.RLock()
        self.run: Run | None = None
        self.tokens: dict[str, dict] = {}
        self.version = 0
        self.base_url = ""
        self._flow = self._load_flow()

    # ------------------------------------------------------------------ состояние

    def _load_flow(self) -> dict:
        try:
            data = json.loads((self.dir / "flow.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        rejected = data.get("rejected") if isinstance(data, dict) else {}
        return {"rejected": {str(k): [str(x) for x in v] for k, v in (rejected or {}).items() if isinstance(v, list)}}

    def _save_flow(self) -> None:
        (self.dir / "flow.json").write_text(json.dumps(self._flow, ensure_ascii=False, indent=1), encoding="utf-8")

    def _touch(self) -> None:
        self.version += 1

    def state(self) -> dict:
        return {"title": self.store.title, "revision": self.store.revision, "version": self.version,
                "run": self.run.public() if self.run else None}

    def graph(self) -> dict:
        return {**self.store.snapshot(), "layout": self.store.layout()}

    def rejected(self, kind: str) -> list[str]:
        return list(self._flow["rejected"].get(kind, []))

    def _reject(self, kind: str, key: str) -> None:
        items = self._flow["rejected"].setdefault(kind, [])
        if key and key not in items:
            items.append(key)
            self._save_flow()

    # ------------------------------------------------------------------ чтение графа и банка

    def cards(self, kind: str, parent: str | None = None) -> list[dict]:
        snap = self.store.snapshot()
        under = None
        if parent:
            under = {e["from"] for e in snap["edges"] if e["to"] == parent and e["kind"] == "детализирует"}
        found = [c for c in snap["cards"] if c["kind"] == kind and (under is None or c["id"] in under)]
        return sorted(found, key=lambda c: int(c["id"][1:]))

    def sphere(self) -> dict | None:
        found = self.cards(flow.SPHERE)
        return found[0] if found else None

    def need_sphere(self) -> dict:
        sphere = self.sphere()
        if sphere is None:
            raise EngineError("no_sphere", "Сначала напишите сферу бизнеса")
        return sphere

    def card(self, card_id: str) -> dict:
        try:
            return self.store.card(card_id)
        except GraphError as error:
            raise EngineError("unknown_card", error.message) from None

    def roles_of(self, scenario_id: str) -> list[dict]:
        snap = self.store.snapshot()
        cards = {c["id"]: c for c in snap["cards"]}
        return [cards[e["to"]] for e in snap["edges"] if e["from"] == scenario_id and e["kind"] == flow.PARTICIPATES and e["to"] in cards]

    def scenario_info(self, card: dict) -> dict:
        usecase = (card.get("attrs") or {}).get("usecase") or {}
        return {"id": card["id"], "title": card["title"], "body": card.get("body", ""),
                "roles": [r["title"] for r in self.roles_of(card["id"])],
                "cases": [c.get("name", "") for c in usecase.get("cases", [])]}

    def records(self, type_: str | None = None) -> list[dict]:
        return self.bank.list(type_)

    def accepted_competitors(self) -> list[dict]:
        return [r for r in self.records("competitor") if (r["data"] or {}).get("state") == "accepted"]

    def facts_of(self, competitor_key: str) -> list[dict]:
        return [r for r in self.records("fact") if (r["data"] or {}).get("competitor") == competitor_key]

    def knowledge(self) -> str:
        requirements = [r["data"].get("text", "") for r in self.records("requirement")]
        names = {r["key"]: r["data"].get("name", r["key"]) for r in self.accepted_competitors()}
        facts = [f"{r['data'].get('kind', 'факт')}: {r['data'].get('text', '')}"
                 + (f" ({names[r['data']['competitor']]})" if r["data"].get("competitor") in names else "")
                 for r in self.records("fact") if (r["data"] or {}).get("competitor") in names or not (r["data"] or {}).get("competitor")]
        sphere = self.need_sphere()
        comps = [f"{d.get('name')} ({d.get('url')})" for d in (r["data"] for r in self.accepted_competitors())]
        return flow.context(sphere["title"], requirements, comps, facts[:80])

    # ------------------------------------------------------------------ действия человека

    def act(self, action: str, d: dict) -> dict:
        handler = getattr(self, "do_" + action, None)
        if not callable(handler) or action.startswith("_"):
            raise EngineError("unknown_action", "Нет такого действия")
        if self.run and self.run.status in ("running", "asking") and action not in ("bank_add", "bank_remove", "bank_update"):
            raise EngineError("busy", "Сейчас работают агенты: дождитесь конца или остановите работу")
        out = handler(d) or {}
        self._touch()
        return out

    def _human(self, ops: list[dict], note: str = "") -> dict:
        try:
            return self.store.apply(ops, HUMAN, note=note)
        except GraphError as error:
            raise EngineError(error.code, error.message) from None

    def do_set_sphere(self, d: dict) -> dict:
        title = _clip(d.get("title"), 200)
        if not title:
            raise EngineError("empty", "Напишите сферу бизнеса")
        sphere = self.sphere()
        if sphere:
            self._human([{"op": "update_card", "id": sphere["id"], "title": title}])
        else:
            self._human([{"op": "add_card", "kind": flow.SPHERE, "title": title}])
        return {}

    def do_accept(self, d: dict) -> dict:
        card = self.card(str(d.get("id") or ""))
        self._human([{"op": "update_card", "id": card["id"], "status": ACCEPTED}])
        return {}

    def _subtree(self, card_id: str) -> list[str]:
        snap = self.store.snapshot()
        out, todo = [], [card_id]
        while todo:
            current = todo.pop()
            out.append(current)
            todo += [e["from"] for e in snap["edges"] if e["to"] == current and e["kind"] == "детализирует"]
        return out[::-1]

    def do_remove(self, d: dict) -> dict:
        """Отклонить или удалить карточку вместе с тем, что под ней; название запоминается, чтобы агент не предлагал снова."""
        card = self.card(str(d.get("id") or ""))
        if card["kind"] == flow.SPHERE:
            raise EngineError("forbidden", "Сферу удалить нельзя: её можно переписать")
        if card["kind"] not in (flow.ASIS, flow.SOLUTION):
            self._reject(card["kind"], card["title"].lower())
        self._human([{"op": "delete_card", "id": i} for i in self._subtree(card["id"])], f"Удалено: {card['title']}")
        if card["kind"] == flow.ROLE:
            self._forget_actor(card["title"])
        return {}

    def _forget_actor(self, name: str) -> None:
        ops = []
        for scenario in self.cards(flow.SCENARIO):
            usecase = (scenario.get("attrs") or {}).get("usecase")
            if not usecase:
                continue
            cases = []
            for case in usecase.get("cases", []):
                actors = [a for a in case.get("actors", []) if a.lower() != name.lower()]
                if actors:
                    cases.append({**case, "actors": actors})
            if cases != usecase.get("cases"):
                ops.append({"op": "update_card", "id": scenario["id"], "attrs": {"usecase": {**usecase, "cases": cases}}})
        if ops:
            self._human(ops)

    def _add_unique(self, kind: str, title: str, body: str, parent: str, **extra) -> str:
        title = _clip(title, 200)
        if not title:
            raise EngineError("empty", "Название не может быть пустым")
        if any(c["title"].lower() == title.lower() for c in self.cards(kind, parent if kind != flow.ROLE else None)):
            raise EngineError("duplicate", f"«{title}» уже есть")
        ops = [{"op": "add_card", "kind": kind, "title": title, "body": str(body or "").strip(), "parent": parent, **extra}]
        return self._human(ops)["ids"][0]

    def do_add_role(self, d: dict) -> dict:
        return {"id": self._add_unique(flow.ROLE, d.get("title"), d.get("body"), self.need_sphere()["id"])}

    def do_add_scenario(self, d: dict) -> dict:
        """Свой сценарий: название и роли; диаграмма вариантов использования строится из них."""
        sphere = self.need_sphere()
        names = [_clip(n, 60) for n in (d.get("roles") or [])]
        roles = {r["title"].lower(): r for r in self.cards(flow.ROLE)}
        found = [roles[n.lower()] for n in names if n.lower() in roles]
        title = _clip(d.get("title"), 200)
        usecase = {"system": title, "cases": [{"name": title, "actors": [r["title"] for r in found]}] if found else []}
        card_id = self._add_unique(flow.SCENARIO, title, d.get("body"), sphere["id"], attrs={"usecase": usecase},
                                   links=[{"to": r["id"], "kind": flow.PARTICIPATES} for r in found])
        return {"id": card_id}

    def do_add_problem(self, d: dict) -> dict:
        scenario = self.card(str(d.get("scenario") or ""))
        return {"id": self._add_unique(flow.PROBLEM, d.get("title"), d.get("body"), scenario["id"],
                                       attrs={"importance": _clip(d.get("importance"), 20)})}

    def do_add_variant(self, d: dict) -> dict:
        problem = self.card(str(d.get("problem") or ""))
        if problem["kind"] != flow.PROBLEM:
            raise EngineError("bad_kind", "Вариант решения привязывается к проблеме")
        return {"id": self._add_unique(flow.VARIANT, d.get("title"), d.get("body"), problem["id"])}

    def do_choose_variant(self, d: dict) -> dict:
        """Выбрать один вариант решения проблемы: он принят, остальные варианты этой проблемы убираются."""
        variant = self.card(str(d.get("id") or ""))
        snap = self.store.snapshot()
        parent = next((e["to"] for e in snap["edges"] if e["from"] == variant["id"] and e["kind"] == "детализирует"), "")
        others = [c["id"] for c in self.cards(flow.VARIANT, parent) if c["id"] != variant["id"]]
        self._human([{"op": "update_card", "id": variant["id"], "status": ACCEPTED}] + [{"op": "delete_card", "id": i} for i in others])
        return {}

    def do_save_steps(self, d: dict) -> dict:
        """Шаги диаграммы последовательности, написанные человеком («1. Кто → Кому: что»): строим по ним диаграмму."""
        scenario = self.card(str(d.get("scenario") or ""))
        kind = str(d.get("kind") or "")
        if kind not in (flow.ASIS, flow.SOLUTION):
            raise EngineError("bad_kind", "Диаграмма бывает «как есть» или «решение»")
        diagram = diagrams.parse_text(str(d.get("text") or ""))
        if not diagram["steps"]:
            raise EngineError("no_steps", "Не вижу шагов. Формат строки: «Кто → Кому: что происходит»")
        existing = self.cards(kind, scenario["id"])
        if existing:
            self._human([{"op": "update_card", "id": existing[0]["id"], "attrs": {"diagram": diagram}}])
        else:
            prefix = "Как есть" if kind == flow.ASIS else "Решение"
            self._human([{"op": "add_card", "kind": kind, "title": f"{prefix}: {scenario['title']}"[:200], "parent": scenario["id"],
                          "attrs": {"diagram": diagram}}])
        return {}

    # ---- банк знаний

    def do_bank_add(self, d: dict) -> dict:
        kind = str(d.get("type") or "")
        text = _clip(d.get("text"), 600)
        if kind == "competitor":
            return self.add_competitor(_clip(d.get("name") or text, 120), _clip(d.get("url"), 300))
        if kind not in ("fact", "requirement") or not text:
            raise EngineError("invalid", "Введите текст факта или требования")
        data = {"text": text} if kind == "requirement" else {"kind": _clip(d.get("kind"), 20) or "факт", "text": text, "competitor": ""}
        self.bank.put(kind, data, HUMAN, key=f"{kind}|{text.lower()}")
        return {}

    def add_competitor(self, name: str, url: str) -> dict:
        key = normalize_url(url)
        if not name or not key:
            raise EngineError("invalid", "Нужны название и сайт конкурента")
        self.bank.put("competitor", {"name": name, "url": url, "why": "", "state": "accepted"}, HUMAN, sources=[url], key=key)
        return {}

    def do_bank_update(self, d: dict) -> dict:
        record = self.bank.get(str(d.get("id") or ""))
        data = dict(record["data"])
        for field in ("text", "name", "url", "why", "kind", "state"):
            if field in d:
                data[field] = _clip(d[field], 600)
        self.bank.update(record["id"], HUMAN, data=data)
        return {}

    def do_bank_remove(self, d: dict) -> dict:
        """Удалить запись; у конкурента вместе с ним уходят его факты, а сайт больше не предлагается."""
        record = self.bank.get(str(d.get("id") or ""))
        if record["type"] == "competitor":
            for fact in self.facts_of(record["key"]):
                self.bank.remove(fact["id"])
            self._reject("competitor", record["key"])
        self.bank.remove(record["id"])
        return {}

    # ------------------------------------------------------------------ запуск агентов

    def start(self, kind: str, ids: list[str] | None = None, redo: bool = False) -> dict:
        if self.run and self.run.status in ("running", "asking"):
            raise EngineError("busy", "Уже идёт работа агентов")
        plan = self._plan(kind, [str(i) for i in (ids or [])], redo)
        run = Run(kind, plan["title"])
        run.total = plan.get("total", 1)
        self.run = run
        self._touch()
        threading.Thread(target=self._work, args=(run, plan), daemon=True).start()
        return {}

    def _plan(self, kind: str, ids: list[str], redo: bool) -> dict:
        sphere = self.need_sphere()
        if kind == "competitors":
            return {"title": "Исследователи ищут конкурентов…", "kind": kind}
        if kind == "analysis":
            targets = self.accepted_competitors() if redo else [
                r for r in self.accepted_competitors() if not (r["data"] or {}).get("analyzed")]
            if not targets:
                raise EngineError("nothing", "Нет принятых конкурентов, которых нужно проанализировать")
            return {"title": "Исследователи анализируют сайты…", "kind": kind, "targets": targets, "redo": redo}
        if kind in ("roles", "scenarios"):
            if kind == "scenarios" and not self.cards(flow.ROLE):
                raise EngineError("no_roles", "Сначала нужны роли")
            return {"title": "Помощник предлагает " + ("роли…" if kind == "roles" else "сценарии…"), "kind": kind, "sphere": sphere}
        if kind == "variants":
            # ids — проблемы (одна или несколько); без ids — все принятые проблемы принятых сценариев
            if ids:
                problems = [self.card(i) for i in ids]
                problems = [p for p in problems if p["kind"] == flow.PROBLEM and p["status"] == ACCEPTED]
            else:
                problems = [p for s in self.cards(flow.SCENARIO) if s["status"] == ACCEPTED for p in self._accepted(flow.PROBLEM, s["id"])]
            if not problems:
                raise EngineError("no_problems", "Сначала примите проблемы сценария")
            return {"title": "Помощник предлагает варианты автоматизации…", "kind": kind, "problems": problems, "total": len(problems)}
        if kind in ("problems", "asis", "solution"):
            scenarios = [self.card(i) for i in ids] if ids else [c for c in self.cards(flow.SCENARIO) if c["status"] == ACCEPTED]
            scenarios = [s for s in scenarios if s["kind"] == flow.SCENARIO]
            if not scenarios:
                raise EngineError("no_scenarios", "Нет принятых сценариев")
            if kind == "asis":
                scenarios = [s for s in scenarios if self._accepted(flow.PROBLEM, s["id"])]
                if not scenarios:
                    raise EngineError("no_problems", "Сначала примите проблемы сценария")
            if kind == "solution":
                scenarios = [s for s in scenarios if self._solved(s["id"])]
                if not scenarios:
                    raise EngineError("no_variant", "Сначала выберите вариант решения хотя бы для одной проблемы")
            names = {"problems": "проблемы", "asis": "диаграмму «как есть»", "solution": "диаграмму «решение»"}
            return {"title": f"Помощник предлагает {names[kind]}…", "kind": kind, "scenarios": scenarios, "total": len(scenarios)}
        raise EngineError("unknown_job", "Нет такой работы")

    def _accepted(self, kind: str, parent_id: str) -> list[dict]:
        return [c for c in self.cards(kind, parent_id) if c["status"] == ACCEPTED]

    def _solved(self, scenario_id: str) -> list[dict]:
        """Принятые проблемы сценария, у которых выбран вариант решения: [{problem, title, body}]."""
        out = []
        for problem in self._accepted(flow.PROBLEM, scenario_id):
            for variant in self._accepted(flow.VARIANT, problem["id"]):
                out.append({"problem": problem["title"], "title": variant["title"], "body": variant["body"]})
        return out

    def _work(self, run: Run, plan: dict) -> None:
        try:
            kind = plan["kind"]
            if kind == "competitors":
                self._job_competitors(run)
            elif kind == "analysis":
                self._job_analysis(run, plan)
            elif kind in ("roles", "scenarios"):
                self._job_global(run, kind)
            elif kind == "variants":
                self._job_variants(run, plan)
            else:
                self._job_scenarios(run, plan)
            if run.stop:
                run.status = "stopped"
            elif run.status != "error":
                run.status = "done"
        except EngineError as error:
            run.status, run.error = "error", error.message
        except Exception as error:  # noqa: BLE001 — любая поломка показывается человеку, а не теряется в потоке
            run.status, run.error = "error", f"Внутренняя ошибка: {error}"
        finally:
            run.question = None
            self._touch()

    def stop(self) -> dict:
        run = self.run
        if run and run.status in ("running", "asking"):
            run.stop = True
            run.answered.set()
            for proc in list(run.procs):
                try:
                    proc.kill()
                except OSError:
                    pass
        return {}

    # ---- вопрос помощника

    def answer(self, question_id: str, choice: dict) -> dict:
        run = self.run
        if not run or not run.question or run.question["id"] != question_id:
            raise EngineError("no_question", "Этот вопрос уже не ждёт ответа")
        run.answer = choice if isinstance(choice, dict) else {}
        run.status = "running"
        run.answered.set()
        self._touch()
        return {}

    @staticmethod
    def _answer_text(question: dict, choice: dict) -> str:
        if choice.get("type") == "rejected":
            return "ни один вариант не подходит" + (f"; пояснение: {choice.get('value')}" if choice.get("value") else "")
        picked = [question["options"][i - 1]["title"] for i in choice.get("indexes", []) if 1 <= i <= len(question["options"])]
        extra = _clip(choice.get("text"), 600)
        return "; ".join(picked + ([f"своё: {extra}"] if extra else [])) or "решай сам"

    # ---- вызов агента

    def _request(self, slot: dict, name: str, system: str, note: str, token: str, session: str, web: bool, run: Run) -> TurnResult:
        adapter = self.adapters.get(slot.get("agent") or "")
        if adapter is None:
            return TurnResult(error=f"Агент «{slot.get('agent')}» не настроен. Откройте «Настройки».")

        def remember(proc):
            run.procs.add(proc)

        request = TurnRequest(
            workdir=self.dir / "agent" / name, system_text=system, note=note,
            env={"IDEA_GRAPH_URL": self.base_url, "IDEA_GRAPH_TOKEN": token}, session_id=session,
            model=slot.get("model") or "", effort=slot.get("effort") or "", web_search=web, on_process=remember,
            extra={"token": token, "project": self})
        return adapter.run(request)

    # ---- исследователи

    def _research(self, run: Run, note: str) -> list[dict]:
        """Три исследователя независимо; возвращает находки всех, кто ответил."""
        reports: list[dict | None] = [None] * RESEARCHERS
        errors: list[str] = []

        def one(index: int) -> None:
            token = secrets.token_urlsafe(24)
            ctx = {"kind": "researcher", "note": note, "report": None}
            self.tokens[token] = ctx
            try:
                result = self._request(self.settings.data["researchers"][index], f"researcher{index + 1}", tools.RESEARCHER_RULES,
                                       note, token, "", True, run)
                if result.error and not run.stop:
                    errors.append(f"Исследователь {index + 1}: {result.error}")
                reports[index] = ctx["report"]
            finally:
                self.tokens.pop(token, None)
                run.done += 1
                self._touch()

        run.total, run.done = RESEARCHERS, 0
        threads = [threading.Thread(target=one, args=(i,), daemon=True) for i in range(RESEARCHERS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        run.notes += errors
        found = [r for r in reports if r]
        if not found and not run.stop:
            raise EngineError("no_report", errors[0] if errors else "Исследователи не прислали результат. Попробуйте ещё раз")
        return [item for r in found for item in r["findings"]]

    def _job_competitors(self, run: Run) -> None:
        sphere = self.need_sphere()
        requirements = [r["data"].get("text", "") for r in self.records("requirement")]
        known = [r["data"].get("name", "") for r in self.records("competitor")]
        note = flow.competitors(sphere["title"], requirements, known, self.rejected("competitor"))
        added = 0
        for item in self._research(run, note):
            url = _clip((item["data"] or {}).get("url") or (item["sources"] or [""])[0], 300)
            key = normalize_url(url)
            if not key or key in self.rejected("competitor") or self.bank.find(key):
                continue
            name = item["title"]
            self.bank.put("competitor", {"name": name, "url": url, "why": item["description"], "state": "proposed"}, AGENT,
                          sources=item["sources"], key=key)
            added += 1
        run.summary = f"Новых конкурентов: {added}"

    def _job_analysis(self, run: Run, plan: dict) -> None:
        sphere = self.need_sphere()
        targets = plan["targets"]
        by_key = {t["key"]: t for t in targets}
        note = flow.analysis(sphere["title"], [(t["data"].get("name", ""), t["data"].get("url", "")) for t in targets])
        findings = self._research(run, note)
        if run.stop:
            return
        if plan.get("redo"):
            for target in targets:
                for fact in self.facts_of(target["key"]):
                    self.bank.remove(fact["id"])
        added = 0
        for item in findings:
            kind = _clip(item["kind"], 20).lower()
            owner = normalize_url((item["data"] or {}).get("competitor", ""))
            if kind not in ("роль", "процесс", "фича") or owner not in by_key:
                continue
            text = item["title"] + (f". {item['description']}" if item["description"] else "")
            self.bank.put("fact", {"kind": kind, "text": text, "competitor": owner}, AGENT, sources=item["sources"],
                          key=f"{owner}|{kind}|{item['title'].lower()}")
            added += 1
        for target in targets:
            self.bank.update(target["id"], AGENT, data={**target["data"], "analyzed": True})
        run.summary = f"Фактов в банке: {added}"

    # ---- помощник

    def _job_global(self, run: Run, kind: str) -> None:
        sphere = self.need_sphere()
        ctx = self.knowledge()
        roles = [c["title"] for c in self.cards(flow.ROLE)]
        if kind == "roles":
            note, allowed = flow.roles(ctx, roles, self.rejected(flow.ROLE)), {flow.ROLE}
        else:
            known = [c["title"] for c in self.cards(flow.SCENARIO)]
            note, allowed = flow.scenarios(ctx, roles, known, self.rejected(flow.SCENARIO)), {flow.SCENARIO}
        self._converse(run, note, allowed, "")

    def _job_scenarios(self, run: Run, plan: dict) -> None:
        kind, ctx = plan["kind"], self.knowledge()
        run.total, run.done = len(plan["scenarios"]), 0
        for scenario in plan["scenarios"]:
            if run.stop:
                return
            info = self.scenario_info(self.card(scenario["id"]))
            problems = [c["title"] + (f": {c['body']}" if c["body"] else "") for c in self._accepted(flow.PROBLEM, scenario["id"])]
            asis = self.cards(flow.ASIS, scenario["id"])
            asis_text = diagrams.to_text(asis[0]["attrs"]["diagram"]) if asis and (asis[0].get("attrs") or {}).get("diagram") else ""
            if kind == "problems":
                note = flow.problems(ctx, info, [c["title"] for c in self.cards(flow.PROBLEM, scenario["id"])],
                                     self.rejected(flow.PROBLEM)); allowed = {flow.PROBLEM}
            elif kind == "asis":
                note, allowed = flow.asis(ctx, info, problems), {flow.ASIS}
            else:
                solved = self._solved(scenario["id"])
                unsolved = [c["title"] for c in self._accepted(flow.PROBLEM, scenario["id"])
                            if c["title"] not in {s["problem"] for s in solved}]
                note, allowed = flow.solution(ctx, info, solved, unsolved, asis_text), {flow.SOLUTION}
            self._converse(run, note, allowed, scenario["id"])
            run.done += 1
            self._touch()

    def _job_variants(self, run: Run, plan: dict) -> None:
        """Варианты решения — по одной проблеме за ход."""
        ctx = self.knowledge()
        run.total, run.done = len(plan["problems"]), 0
        snap = self.store.snapshot()
        for problem in plan["problems"]:
            if run.stop:
                return
            parent = next((e["to"] for e in snap["edges"] if e["from"] == problem["id"] and e["kind"] == "детализирует"), "")
            info = self.scenario_info(self.card(parent))
            asis = self.cards(flow.ASIS, parent)
            asis_text = diagrams.to_text(asis[0]["attrs"]["diagram"]) if asis and (asis[0].get("attrs") or {}).get("diagram") else ""
            note = flow.variants(ctx, info, {"title": problem["title"], "body": problem["body"]}, asis_text,
                                 [c["title"] for c in self.cards(flow.VARIANT, problem["id"])], self.rejected(flow.VARIANT))
            self._converse(run, note, {flow.VARIANT}, problem["id"])
            run.done += 1
            self._touch()

    def _converse(self, run: Run, note: str, allowed: set, target: str) -> None:
        """Ходы помощника, пока он не закончит; в спорной ситуации он спрашивает человека и продолжает с его ответом."""
        session, task = "", note
        while True:
            token = secrets.token_urlsafe(24)
            ctx = {"kind": "helper", "run": run, "allowed": allowed, "target": target, "asked": False, "note": note, "created": 0}
            self.tokens[token] = ctx
            run.answered.clear()
            try:
                result = self._request(self.settings.data["helper"], "helper", tools.HELPER_RULES, note, token, session, False, run)
            finally:
                self.tokens.pop(token, None)
            if run.stop:
                return
            if result.error:
                raise EngineError("agent_error", result.error)
            session = result.session_id or session
            if ctx["asked"]:
                run.status = "asking"
                self._touch()
                run.answered.wait()
                if run.stop:
                    return
                question = run.question
                note = (f"Ответ человека на твой вопрос «{question['question']}»: {self._answer_text(question, run.answer or {})}. "
                        "Продолжи работу с учётом ответа.\n\nЗадача прежняя:\n" + task)
                run.question = None
                continue
            if not ctx["created"]:
                run.notes.append("Помощник ничего не добавил" + (f": {result.text[:200]}" if result.text else ""))
            return

    # ------------------------------------------------------------------ инструменты агентов

    def tools_for(self, token: str) -> list[dict]:
        ctx = self.tokens.get(token)
        if ctx is None:
            raise EngineError("bad_token", "Ход уже закончен: инструменты недоступны")
        return tools.RESEARCH_TOOLS if ctx["kind"] == "researcher" else tools.HELPER_TOOLS

    def tool_call(self, token: str, tool: str, args: dict) -> str:
        ctx = self.tokens.get(token)
        if ctx is None:
            raise EngineError("bad_token", "Ход уже закончен: инструменты недоступны")
        args = args if isinstance(args, dict) else {}
        if tool == "turn_info":
            return ctx["note"]
        if ctx["kind"] == "researcher":
            if tool != "report":
                raise EngineError("unknown_tool", "У исследователя есть только turn_info и report")
            return self._tool_report(ctx, args)
        if tool == "graph_apply":
            return self._tool_apply(ctx, args)
        if tool == "ask":
            return self._tool_ask(ctx, args)
        if tool == "finish":
            ctx["run"].summary = _clip(args.get("summary"), 400)
            return "Принято."
        raise EngineError("unknown_tool", "Есть только graph_apply, ask, finish и turn_info")

    @staticmethod
    def _tool_report(ctx: dict, args: dict) -> str:
        findings = []
        for item in args.get("findings") or []:
            if not isinstance(item, dict) or not _clip(item.get("title"), 200):
                continue
            data = item.get("data")
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except ValueError:
                    data = {}
            sources = item.get("sources") or []
            findings.append({"kind": _clip(item.get("kind"), 40), "title": _clip(item.get("title"), 200),
                             "description": _clip(item.get("description"), 400), "data": data if isinstance(data, dict) else {},
                             "sources": [_clip(s, 300) for s in (sources if isinstance(sources, list) else [sources]) if _clip(s, 300)][:5]})
        if not findings:
            raise EngineError("empty_report", "В отчёте нет находок. Найди хотя бы что-то подтверждённое")
        ctx["report"] = {"findings": findings}
        return "Принято. Заверши ход."

    def _tool_ask(self, ctx: dict, args: dict) -> str:
        if ctx["asked"]:
            raise EngineError("one_question", "За ход можно задать только один вопрос")
        options = [{"title": _clip(o.get("title"), 200), "consequence": _clip(o.get("consequence"), 200)}
                   for o in args.get("options") or [] if isinstance(o, dict) and _clip(o.get("title"), 200)][:4]
        question = _clip(args.get("question"), 300)
        if not question or len(options) < 2:
            raise EngineError("bad_question", "Нужен вопрос и от двух вариантов ответа")
        recommended = [i for i in (args.get("recommended") or []) if isinstance(i, int) and 1 <= i <= len(options)]
        ctx["run"].question = {"id": secrets.token_urlsafe(6), "question": question, "options": options,
                               "recommended": recommended, "reason": _clip(args.get("reason"), 200)}
        ctx["asked"] = True
        return "Вопрос задан. Заверши ход: ответ придёт в следующем ходе."

    def _tool_apply(self, ctx: dict, args: dict) -> str:
        ops = args.get("ops")
        if not isinstance(ops, list) or not ops:
            raise EngineError("invalid_ops", "Ожидался непустой список операций")
        sphere = self.need_sphere()
        roles = {r["title"].lower(): r for r in self.cards(flow.ROLE)}
        build, skipped = [], []
        for number, op in enumerate(ops, 1):
            if not isinstance(op, dict) or op.get("op") != "add_card":
                raise EngineError("bad_op", f"Операция {number}: разрешён только add_card")
            kind = str(op.get("kind") or "")
            if kind not in ctx["allowed"]:
                raise EngineError("bad_kind", f"Операция {number}: сейчас можно создавать только карточки вида: {', '.join(sorted(ctx['allowed']))}")
            title = _clip(op.get("title"), 200)
            if not title:
                raise EngineError("bad_title", f"Операция {number}: нужен title")
            parent = ctx["target"] or sphere["id"]
            if title.lower() in self.rejected(kind) or any(
                    c["title"].lower() == title.lower() for c in self.cards(kind, parent if ctx["target"] else None)):
                skipped.append(title)
                continue
            attrs, links, status = {}, [], PROPOSED
            raw = op.get("attrs") if isinstance(op.get("attrs"), dict) else {}
            if kind == flow.SCENARIO:
                try:
                    usecase = diagrams.normalize_usecase(raw.get("usecase"))
                except ValueError as error:
                    raise EngineError("bad_usecase", f"Операция {number}: {error}") from None
                if not usecase["cases"]:
                    raise EngineError("bad_usecase", f"Операция {number}: в attrs.usecase нужны cases с name и actors (роли)")
                unknown = sorted({a for c in usecase["cases"] for a in c["actors"] if a.lower() not in roles})
                if unknown:
                    raise EngineError("bad_actor", f"Операция {number}: актёры {', '.join(unknown)} — не роли. Роли: "
                                      + ", ".join(r["title"] for r in roles.values()))
                usecase["system"] = usecase["system"] or title
                actors = {a.lower() for c in usecase["cases"] for a in c["actors"]}
                links = [{"to": roles[a]["id"], "kind": flow.PARTICIPATES} for a in actors]
                attrs = {"usecase": usecase}
            elif kind in (flow.ASIS, flow.SOLUTION):
                try:
                    diagram = diagrams.normalize({"participants": (raw.get("diagram") or {}).get("participants"),
                                                  "steps": (raw.get("diagram") or {}).get("steps")})
                except (ValueError, AttributeError) as error:
                    raise EngineError("bad_diagram", f"Операция {number}: {error}") from None
                if len(diagram["steps"]) < 2:
                    raise EngineError("bad_diagram", f"Операция {number}: в attrs.diagram нужны participants и минимум два шага steps (from, to, text)")
                attrs, status = {"diagram": diagram}, ACCEPTED
                for old in self.cards(kind, parent):
                    build.append({"op": "delete_card", "id": old["id"]})
            elif kind == flow.PROBLEM:
                attrs = {"importance": _clip(raw.get("importance"), 20).lower()}
            build.append({"op": "add_card", "kind": kind, "title": title, "body": str(op.get("body") or "").strip()[:4000],
                          "status": status, "attrs": attrs, "parent": parent, "links": links})
        created = 0
        if build:
            try:
                result = self.store.apply(build, AGENT, note="Предложено помощником")
            except GraphError as error:
                raise EngineError(error.code, error.message) from None
            created = len(result["ids"])
            ctx["created"] += created
            self._touch()
        return f"Добавлено карточек: {created}." + (f" Уже есть или отклонялись, пропущено: {', '.join(skipped)}." if skipped else "")
