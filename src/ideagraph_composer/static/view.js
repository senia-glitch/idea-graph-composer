/* Экран «Конвейер»: карта пакетов на том же холсте, что и граф, и прогон по ней. */

const ALWAYS = "*";
const STATE_TEXT = {idle: "не запускалась", running: "идёт", waiting_next: "ждёт «Дальше»", done: "пройдена", stopped: "остановлена"};

export function mount(root, host) {
  const {esc, toast, act, call, createCanvas} = host;
  const V = {maps: [], packages: [], hasDecider: false, map: null, dirty: false, state: null, runs: {}, problems: null, timer: 0};

  root.innerHTML = `
    <style>
      .composer{display:flex;flex-direction:column;flex:1;min-width:0;min-height:0}
      .cbar{display:flex;gap:6px;align-items:center;padding:7px 10px;border-bottom:1px solid #000;background:var(--panel);flex-wrap:wrap}
      .cbar select,.cbar input{width:220px}
      .cmain{display:flex;flex:1;min-height:0}
      .cside{width:250px;flex:none;background:var(--panel);border-right:1px solid #000;padding:8px 10px;overflow:auto}
      .cinsp{width:290px;flex:none;background:var(--panel);border-left:1px solid #000;padding:10px 12px;overflow:auto}
      .pal{display:block;width:100%;text-align:left;margin:4px 0}
      #cHistory>div{margin:5px 0;color:var(--txt)}
    </style>
    <div class="composer">
      <div class="cbar">
        <select id="cMap" title="Карта"></select>
        <input id="cName" placeholder="Название карты" maxlength="120">
        <button id="cNew" title="Новая пустая карта">Новая</button>
        <button id="cDefault" title="Собрать карту из установленных пакетов">По умолчанию</button>
        <button id="cSave">Сохранить</button>
        <button id="cDelete" class="danger">Удалить</button>
        <span class="spacer"></span>
        <span id="cStatus" class="hint"></span>
        <button id="cRun" class="primary" title="Идти по карте без остановок">▶ Запустить</button>
        <button id="cStep" title="Останавливаться после каждого шага">По шагам</button>
        <button id="cNext" class="primary" hidden>Дальше ▶</button>
        <button id="cStop" hidden>■ Стоп</button>
      </div>
      <div class="cmain">
        <aside class="cside">
          <div class="lbl">Пакеты</div>
          <div id="cPalette"></div>
          <div class="hint">Нажмите на пакет, чтобы добавить шаг. Потяните кружок ○ от шага к шагу, чтобы провести стрелку.</div>
          <div id="cProblems"></div>
          <div class="lbl">Ход прогона</div>
          <div id="cHistory" class="hint">Пока пусто.</div>
        </aside>
        <div id="cCanvas"></div>
        <aside class="cinsp" id="cInsp" hidden></aside>
      </div>
    </div>`;
  const $ = (id) => root.querySelector("#" + id);
  const pkg = (id) => V.packages.find((p) => p.id === id);
  const node = (id) => V.map?.nodes.find((n) => n.id === id);

  const canvas = createCanvas($("cCanvas"), {
    renderNode: (n) => {
      const p = pkg(n.package);
      const run = V.runs[n.package];
      const current = V.state && ["running", "waiting_next"].includes(V.state.status) && V.state.current === n.id;
      const done = V.state?.history?.filter((h) => h.node === n.id).at(-1);
      return `<div class="hd" style="--c:${p ? (p.kind === "code" ? "#7fc97f" : "#5aa0ff") : "#ff5d5d"}"><span class="grip">⠿</span>` +
        `<span class="chip">${p ? (p.kind === "code" ? "без модели" : "агент") : "нет пакета"}</span>` +
        (V.map.start === n.id ? '<span class="chip todo">старт</span>' : "") +
        `<span class="t">${esc(p?.name || n.package)}</span><span class="link-handle" title="Потяните к следующему шагу">○</span></div>` +
        `<div class="body">${esc(p?.description || "Пакет не установлен")}</div>` +
        `<div class="badges">${(p?.outcomes || []).map((o) => `<span class="chip">${esc(o)}</span>`).join("")}` +
        (p?.asks ? `<span class="chip ${n.answerer === "decider" ? "agent" : ""}">отвечает: ${n.answerer === "decider" ? "решатель" : n.answerer === "human" ? "я" : "как в графе"}</span>` : "") +
        (current ? `<span class="chip warn">сейчас: ${esc(run?.status === "asking" ? "ждёт вашего ответа" : "работает")}</span>` : "") +
        (done && !current ? `<span class="chip">✓ ${esc(done.outcome)}</span>` : "") + "</div>";
    },
    nodeClass: (n) => (pkg(n.package) ? "" : "planned"),
    edgeLabel: (e) => (e.when === ALWAYS ? "всегда" : e.when) + (V.state?.loops?.[e.id] ? ` · ${V.state.loops[e.id]}/${e.max_loops}` : ""),
    edgeColor: (e) => (e.when === "есть_мешающие" ? "#ff5d5d" : e.when === ALWAYS ? "#9a9aa2" : "#7fc97f"),
    edgeDashed: (e) => e.when === ALWAYS,
    onSelect: () => renderInspector(),
    onEdgeSelect: () => renderInspector(),
    onMoved: () => { syncPositions(); },
    onLink: (from, to) => {
      const outcomes = pkg(node(from).package)?.outcomes || [];
      const used = V.map.edges.filter((e) => e.from === from).map((e) => e.when);
      const when = outcomes.find((o) => !used.includes(o)) || (used.includes(ALWAYS) ? outcomes[0] || ALWAYS : ALWAYS);
      const id = "a" + (Math.max(0, ...V.map.edges.map((e) => Number(e.id.slice(1)) || 0)) + 1);
      V.map.edges.push({id, from, to, when, max_loops: 3});
      changed();
      canvas.state.selectedEdge = id;
      canvas.state.selected = null;
      draw();
      renderInspector();
    },
  });

  function syncPositions() {
    if (!V.map) return;
    const layout = canvas.layout().positions;
    let moved = false;
    for (const n of V.map.nodes) {
      const p = layout[n.id];
      if (p && (p.x !== n.x || p.y !== n.y)) { n.x = p.x; n.y = p.y; moved = true; }
    }
    if (moved) { V.dirty = true; renderBar(); }
  }
  function changed() { V.dirty = true; V.problems = null; renderBar(); validateSoon(); }
  function draw() {
    if (!V.map) return;
    canvas.setData(V.map.nodes.map((n) => ({...n})), V.map.edges.map((e) => ({...e})));
  }
  function openMap(map) {
    V.map = JSON.parse(JSON.stringify(map));
    V.dirty = false;
    canvas.state.selected = null;
    canvas.state.selectedEdge = null;
    canvas.setLayout({positions: Object.fromEntries(V.map.nodes.map((n) => [n.id, {x: n.x, y: n.y}]))});
    canvas.state.viewTouched = false;
    draw();
    canvas.fit();
    renderBar(); renderInspector(); validateSoon();
  }

  function renderBar() {
    $("cMap").innerHTML = V.maps.map((m) => `<option value="${esc(m.id)}" ${m.id === V.map?.id ? "selected" : ""}>${esc(m.name)}</option>`).join("") +
      (V.map && !V.maps.some((m) => m.id === V.map.id) ? `<option selected value="${esc(V.map.id)}">${esc(V.map.name)} (не сохранена)</option>` : "");
    if (document.activeElement !== $("cName")) $("cName").value = V.map?.name || "";
    $("cSave").textContent = V.dirty ? "Сохранить *" : "Сохранить";
    const status = V.state?.status || "idle", active = status === "running" || status === "waiting_next";
    $("cStatus").textContent = host.S.project ? `Карта ${STATE_TEXT[status] || status}${V.state?.reason ? " — " + V.state.reason : ""}` : "Откройте идею, чтобы запустить карту";
    $("cRun").hidden = $("cStep").hidden = active;
    $("cStop").hidden = !active;
    $("cNext").hidden = status !== "waiting_next";
    $("cRun").disabled = $("cStep").disabled = !host.S.project;
    $("cPalette").innerHTML = V.packages.map((p) => `<button class="pal" data-add="${p.id}" title="${esc(p.description)}">+ ${esc(p.name)}</button>`).join("");
    const problems = V.problems;
    $("cProblems").innerHTML = problems
      ? problems.errors.map((e) => `<div class="err">${esc(e)}</div>`).join("") + problems.warnings.map((w) => `<div class="warn-box">${esc(w)}</div>`).join("") +
        (!problems.errors.length && !problems.warnings.length ? '<div class="hint">Карта в порядке.</div>' : "")
      : "";
    const history = V.state?.history || [];
    $("cHistory").innerHTML = history.length
      ? history.map((h, i) => `<div>${i + 1}. <b>${esc(pkg(h.package)?.name || h.package)}</b> → ${esc(h.outcome)}${h.summary ? `<div class="hint">${esc(h.summary.slice(0, 160))}</div>` : ""}</div>`).join("")
      : "Пока пусто.";
  }

  function renderInspector() {
    const box = $("cInsp"), st = canvas.state;
    const edge = st.selectedEdge && V.map.edges.find((e) => e.id === st.selectedEdge);
    const n = st.selected && node(st.selected);
    if (edge) {
      const outcomes = pkg(node(edge.from).package)?.outcomes || [];
      box.hidden = false;
      box.innerHTML = `<div class="insp-head"><b>Стрелка</b></div>
        <div class="hint">${esc(pkg(node(edge.from).package)?.name || "")} → ${esc(pkg(node(edge.to).package)?.name || "")}</div>
        <label>Срабатывает при итоге<select id="eWhen">${[ALWAYS, ...outcomes].map((o) => `<option value="${esc(o)}" ${o === edge.when ? "selected" : ""}>${o === ALWAYS ? "любом (всегда)" : esc(o)}</option>`).join("")}</select></label>
        <label>Сколько раз можно пройти за прогон<input id="eLoops" type="number" min="1" max="50" value="${edge.max_loops}"></label>
        <div class="row-actions"><button id="eDel" class="danger">Удалить стрелку</button></div>`;
      $("eWhen").onchange = (e) => { edge.when = e.target.value; changed(); draw(); };
      $("eLoops").onchange = (e) => { edge.max_loops = Math.max(1, Number(e.target.value) || 3); changed(); draw(); };
      $("eDel").onclick = () => { V.map.edges = V.map.edges.filter((x) => x.id !== edge.id); st.selectedEdge = null; changed(); draw(); renderInspector(); };
      return;
    }
    if (!n) { box.hidden = true; return; }
    const p = pkg(n.package);
    box.hidden = false;
    box.innerHTML = `<div class="insp-head"><b>Шаг: ${esc(p?.name || n.package)}</b></div>
      <div class="hint">${esc(p?.description || "")}</div>
      <div class="hint">Читает: ${esc((p?.reads || []).join(", ") || "—")}<br>Создаёт: ${esc((p?.writes || []).join(", ") || "—")}<br>Модель: ${esc(p?.model_level || "—")}</div>
      ${p?.asks ? `<label>Кто отвечает на вопросы этого шага<select id="nWho">
        <option value="" ${n.answerer === "" ? "selected" : ""}>как выбрано на экране «Граф»</option>
        <option value="human" ${n.answerer === "human" ? "selected" : ""}>я</option>
        <option value="decider" ${n.answerer === "decider" ? "selected" : ""} ${V.hasDecider ? "" : "disabled"}>решатель${V.hasDecider ? "" : " (не установлен)"}</option></select></label>` : ""}
      <div class="row-actions"><button id="nStart" ${V.map.start === n.id ? "disabled" : ""}>Сделать первым</button><button id="nDel" class="danger">Убрать шаг</button></div>`;
    if ($("nWho")) $("nWho").onchange = (e) => { n.answerer = e.target.value; changed(); draw(); };
    $("nStart").onclick = () => { V.map.start = n.id; changed(); draw(); renderInspector(); };
    $("nDel").onclick = () => {
      V.map.nodes = V.map.nodes.filter((x) => x.id !== n.id);
      V.map.edges = V.map.edges.filter((e) => e.from !== n.id && e.to !== n.id);
      if (V.map.start === n.id) V.map.start = V.map.nodes[0]?.id || "";
      st.selected = null; changed(); draw(); renderInspector();
    };
  }

  let validateTimer = 0;
  function validateSoon() {
    clearTimeout(validateTimer);
    validateTimer = setTimeout(async () => {
      if (!V.map) return;
      try { V.problems = await call("validate", {map: V.map}); } catch (error) { V.problems = {errors: [error.message], warnings: []}; }
      renderBar();
    }, 250);
  }

  async function save() {
    syncPositions();
    const result = await call("save", {map: V.map});
    V.map = result.map;
    V.problems = {errors: result.errors, warnings: result.warnings};
    V.dirty = false;
    const index = V.maps.findIndex((m) => m.id === V.map.id);
    if (index >= 0) V.maps[index] = result.map; else V.maps.push(result.map);
    renderBar();
  }

  root.querySelector(".cbar").addEventListener("click", (event) => {
    const id = event.target.id;
    if (id === "cNew") {
      openMap({id: "karta-" + Date.now().toString(36), name: "Новая карта", start: "", nodes: [], edges: []});
      V.dirty = true; renderBar();
    }
    if (id === "cDefault") act(async () => { const data = await call("default", {}); openMap({...data.map, id: V.map?.id || data.map.id, name: V.map?.name || data.map.name}); V.dirty = true; renderBar(); });
    if (id === "cSave") act(async () => { await save(); toast("Карта сохранена"); });
    if (id === "cDelete" && V.map) act(async () => {
      await call("delete", {id: V.map.id});
      V.maps = V.maps.filter((m) => m.id !== V.map.id);
      await load(true);
    });
    if (id === "cRun" || id === "cStep") act(async () => {
      syncPositions();
      const data = await call("run", {map: V.map, mode: id === "cStep" ? "step" : "auto"});
      V.state = data.state; V.dirty = false;
      toast(id === "cStep" ? "Карта запущена по шагам. Вопросы пакетов — на экране «Граф»." : "Карта запущена. Вопросы пакетов — на экране «Граф».");
      refresh();
    });
    if (id === "cNext") act(async () => { V.state = (await call("next", {})).state; refresh(); });
    if (id === "cStop") act(async () => { V.state = (await call("stop", {})).state; refresh(); });
  });
  $("cMap").onchange = (e) => { const map = V.maps.find((m) => m.id === e.target.value); if (map) openMap(map); };
  $("cName").oninput = (e) => { if (V.map) { V.map.name = e.target.value; V.dirty = true; $("cSave").textContent = "Сохранить *"; } };
  $("cPalette").onclick = (e) => {
    const id = e.target.dataset.add;
    if (!id || !V.map) return;
    const nodeId = "n" + (Math.max(0, ...V.map.nodes.map((n) => Number(n.id.slice(1)) || 0)) + 1);
    const last = V.map.nodes.at(-1);
    V.map.nodes.push({id: nodeId, package: id, answerer: "", x: last ? last.x + 360 : 60, y: last ? last.y : 80});
    if (!V.map.start) V.map.start = nodeId;
    canvas.placeAt(nodeId, {x: last ? last.x + 360 : 60, y: last ? last.y : 80});
    changed(); draw();
  };

  async function refresh() {
    try {
      const data = await call("state");
      const before = JSON.stringify([V.state, Object.values(V.runs).map((r) => r.status)]);
      V.state = data.state; V.runs = data.runs || {};
      if (before !== JSON.stringify([V.state, Object.values(V.runs).map((r) => r.status)])) { renderBar(); draw(); }
    } catch (error) { /* оболочка недоступна — покажет основной экран */ }
  }
  async function load(force) {
    const data = await call("maps");
    V.maps = data.maps; V.packages = data.packages; V.hasDecider = data.has_decider;
    if (force || !V.map) openMap(V.maps.find((m) => m.id === V.state?.map?.id) || V.maps[0]);
    renderBar();
  }

  return {
    show() {
      act(async () => { await refresh(); await load(false); renderBar(); draw(); });
      clearInterval(V.timer);
      V.timer = setInterval(() => { if (root.classList.contains("on")) refresh(); }, 1000);
    },
  };
}
