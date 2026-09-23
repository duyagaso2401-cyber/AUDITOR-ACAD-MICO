/* =========================================================================
   Auditor Académico – lógica del dashboard (JavaScript sin dependencias)
   ========================================================================= */
(() => {
  "use strict";

  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmt = (n, d = 1) => (n === null || n === undefined || Number.isNaN(+n)) ? "—" : (+n).toLocaleString("es", { maximumFractionDigits: d });

  const state = {
    report: null,             // última respuesta de /api/v1/audit
    rewrites: new Map(),      // index -> {original, rewritten, changes, accepted}
    rewriteStatus: new Map(), // index -> pendiente | en_proceso | reescrito | sin_cambios | fallido
    applied: false,
    projectId: null,          // UUID del proyecto en el servidor
    revision: null,           // revisión conocida (control de concurrencia)
    status: "borrador",       // borrador | auditado | reescribiendo | pausado | finalizado
    pausedUntil: 0,           // epoch ms sugerido para reanudar tras cuota agotada
    createdAt: null,
  };

  /* ---------------- Tema ---------------- */
  const root = document.documentElement;
  try { const t = localStorage.getItem("theme"); if (t) root.dataset.theme = t; } catch (_) {}
  $("#themeToggle").addEventListener("click", () => {
    const dark = root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
    root.dataset.theme = dark ? "light" : "dark";
    try { localStorage.setItem("theme", root.dataset.theme); } catch (_) {}
  });

  /* ---------------- Utilidades UI ---------------- */
  let toastTimer;
  function toast(msg, ms = 3500) {
    const t = $("#toast"); t.textContent = msg; t.classList.remove("hidden");
    clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.add("hidden"), ms);
  }

  /** Llamada a la API. Los errores llevan .status, .retryAfter y .retryable para decidir reintentos. */
  async function api(path, body, { raw = false, signal } = {}) {
    const headers = { "X-Client-Id": clientId() };
    const opts = body === undefined ? { method: "GET", headers }
      : body instanceof FormData ? { method: "POST", headers, body }
      : { method: "POST", headers: { ...headers, "Content-Type": "application/json" }, body: JSON.stringify(body) };
    let res;
    try {
      res = await fetch(path, { credentials: "same-origin", signal, ...opts });
    } catch (e) {
      if (e.name === "AbortError") throw e;
      throw Object.assign(new Error("Sin conexión con el servidor"), { status: 0, retryable: true });
    }
    if (!res.ok) {
      let data = {};
      try { data = await res.json(); } catch (_) {}          // p. ej. 502/504 HTML del proxy
      const retryAfter = +(res.headers.get("Retry-After") || data.retry_after || 0);
      throw Object.assign(new Error(data.error || `Error ${res.status}`), {
        status: res.status, retryAfter, data,
        retryable: data.retryable ?? [0, 429, 500, 502, 503, 504].includes(res.status),
      });
    }
    return raw ? res : res.json();
  }

  const apiGet = (path, opts) => api(path, undefined, opts);

  const sleep = (ms, signal) => new Promise((resolve, reject) => {
    const t = setTimeout(resolve, ms);
    signal?.addEventListener("abort", () => { clearTimeout(t); reject(new DOMException("Cancelado", "AbortError")); }, { once: true });
  });

  function riskColor(v, thr, invert = false) {
    const css = getComputedStyle(root);
    if (invert) v = 100 - v, thr = 100 - thr;
    if (v <= thr) return css.getPropertyValue("--ok").trim();
    if (v <= Math.max(thr * 2, 60)) return css.getPropertyValue("--warn").trim();
    return css.getPropertyValue("--danger").trim();
  }

  /* =========================================================================
     PERSISTENCIA DE PROYECTOS
     - localStorage: copia inmediata en cada cambio relevante (auditoría, cada lote
       reescrito, ediciones). Sobrevive a recargas y cierres del navegador.
     - Servidor (/api/v1/projects/save): al auditar, al pausar por cuota, al terminar,
       al exportar y con el botón "Guardar avance". Preparado para la BD institucional.
     ========================================================================= */
  const DRAFT_KEY = "auditor:draft:v1";
  const CLIENT_KEY = "auditor:client_id";

  const ls = {
    get(k) { try { return localStorage.getItem(k); } catch (_) { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); return true; } catch (_) { return false; } },
    del(k) { try { localStorage.removeItem(k); } catch (_) {} },
  };

  let _clientId = null;
  function clientId() {        // identificador anónimo y estable de este navegador
    if (_clientId) return _clientId;
    _clientId = ls.get(CLIENT_KEY);
    if (!_clientId || !/^[A-Za-z0-9-]{16,64}$/.test(_clientId)) {
      _clientId = (crypto.randomUUID ? crypto.randomUUID()
        : Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) => b.toString(16).padStart(2, "0")).join(""));
      ls.set(CLIENT_KEY, _clientId);
    }
    return _clientId;
  }

  const readContext = () => Object.fromEntries($$("[data-ctx]").map((el) => [el.dataset.ctx, el.value.trim() || null]));
  const writeContext = (ctx = {}) => $$("[data-ctx]").forEach((el) => { el.value = ctx[el.dataset.ctx] || ""; });

  /** Recorta partes pesadas y regenerables del informe si el almacenamiento local se llena. */
  function compactReport(r) {
    if (!r) return r;
    const c = JSON.parse(JSON.stringify(r));
    delete c.ai?.per_sentence;                       // duplicado en sentences[]
    if (c.ai?.local) delete c.ai.local.sentence_perplexities;
    (c.plagiarism?.fragments || []).forEach((f) => (f.matches || []).forEach((m) => { m.abstract = (m.abstract || "").slice(0, 120); }));
    (c.plagiarism?.sources || []).forEach((m) => { m.abstract = (m.abstract || "").slice(0, 160); });
    return c;
  }

  /** Estado completo del proyecto (modelo compartido con /api/v1/projects/save). */
  function buildSnapshot({ compact = false } = {}) {
    const items = {};
    const indices = new Set([...state.rewriteStatus.keys(), ...state.rewrites.keys()]);
    indices.forEach((i) => {
      const w = state.rewrites.get(i);
      let st = state.rewriteStatus.get(i) || (w ? "reescrito" : "pendiente");
      if (st === "en_proceso") st = "pendiente";     // un lote interrumpido vuelve a la cola
      items[i] = { status: st, ...(w ? { original: w.original, rewritten: w.rewritten, changes: w.changes || [], accepted: !!w.accepted } : {}) };
    });
    return {
      id: state.projectId, revision: state.revision, status: state.status,
      title: $("#docTitle").value.trim() || null,
      saved_at: new Date().toISOString(), created_at: state.createdAt,
      context: readContext(),
      document: {
        text: textInput.value, title: $("#docTitle").value, author: $("#docAuthor").value,
        norma: instSel.value, file_name: $("#fileName").dataset.name || null,
        options: { provider: $("#llmProvider").value, use_semantic: $("#optGemini").checked,
                   check_plagiarism: $("#optPlag").checked,
                   max_fragments: +$("#optFragments").value, mode: $("#rwMode").value },
      },
      report: compact ? compactReport(state.report) : state.report,
      rewrite: { mode: $("#rwMode").value, applied: state.applied, paused_until: state.pausedUntil || null, items },
    };
  }

  let draftDecisionPending = false;   // banner visible: no sobrescribir el borrador anterior
  const hasContent = () => !draftDecisionPending && !!(state.report || textInput.value.trim().length >= 40);
  let saveTimer = null, lastLocal = null, lastServer = null, serverError = null;

  function saveLocal() {
    clearTimeout(saveTimer); saveTimer = null;
    if (!hasContent()) return false;
    if (!state.createdAt) state.createdAt = new Date().toISOString();
    let ok = ls.set(DRAFT_KEY, JSON.stringify(buildSnapshot()));
    if (!ok) ok = ls.set(DRAFT_KEY, JSON.stringify(buildSnapshot({ compact: true })));  // cuota del navegador
    if (ok) lastLocal = new Date();
    updateSaveStatus(ok ? null : "No se pudo guardar en este navegador (almacenamiento lleno)");
    return ok;
  }
  const scheduleSave = () => { clearTimeout(saveTimer); saveTimer = setTimeout(saveLocal, 800); };

  async function saveServer({ silent = true } = {}) {
    if (!hasContent()) { if (!silent) toast("No hay nada que guardar todavía."); return false; }
    saveLocal();
    try {
      const r = await api("/api/v1/projects/save", buildSnapshot());
      state.projectId = r.project.id; state.revision = r.project.revision;
      lastServer = new Date(); serverError = null;
      saveLocal();                                     // persiste id y revisión
      if (!silent) toast("Avance guardado en este navegador y en el servidor.");
      return true;
    } catch (err) {
      serverError = err.status === 409 ? "Otra pestaña guardó una versión más reciente" : err.message;
      updateSaveStatus();
      if (!silent) toast(`Guardado sólo en este navegador. Servidor: ${serverError}`, 6000);
      return false;
    }
  }

  function updateSaveStatus(localError = null) {
    const el = $("#saveStatus"); if (!el) return;
    const t = (d) => d.toLocaleTimeString("es", { hour: "2-digit", minute: "2-digit" });
    el.classList.toggle("err", !!(localError || serverError));
    el.textContent = localError || (lastLocal
      ? `Guardado ${t(lastLocal)}${lastServer ? " · servidor ✓" : ""}${serverError ? " · servidor ✕" : ""}` : "");
    el.title = serverError ? `Servidor: ${serverError}` : "";
  }

  function clearProject() {
    ls.del(DRAFT_KEY);
    Object.assign(state, { report: null, applied: false, projectId: null, revision: null,
                           status: "borrador", pausedUntil: 0, createdAt: null });
    state.rewrites.clear(); state.rewriteStatus.clear();
    textInput.value = ""; updateCount(); $("#docTitle").value = ""; $("#docAuthor").value = "";
    $("#fileName").textContent = ""; delete $("#fileName").dataset.name; writeContext({});
    $("#report").classList.add("hidden"); $("#emptyState").classList.remove("hidden");
    lastLocal = lastServer = serverError = null; updateSaveStatus();
  }

  /** Restaura un proyecto (local o del servidor) y deja lista la reanudación. */
  function restoreProject(p) {
    const d = p.document || {};
    textInput.value = d.text || ""; updateCount();
    $("#docTitle").value = d.title || p.title || ""; $("#docAuthor").value = d.author || "";
    if (d.norma && instSel.querySelector(`option[value="${CSS.escape(d.norma)}"]`)) { instSel.value = d.norma; updateInstNotes(); }
    if (d.options?.mode) $("#rwMode").value = d.options.mode;
    if (d.options?.provider && $(`#llmProvider option[value="${d.options.provider}"]`)) $("#llmProvider").value = d.options.provider;
    if (d.file_name) { $("#fileName").textContent = `${d.file_name} (restaurado)`; $("#fileName").dataset.name = d.file_name; }
    const ctx = { ...(p.context || {}) };               // borradores antiguos: asignatura -> área/asignatura
    if (!ctx.area_asignatura && ctx.asignatura) ctx.area_asignatura = ctx.asignatura;
    writeContext(ctx);
    if (Object.values(ctx).some(Boolean)) $("#contextBox").open = true;

    Object.assign(state, { projectId: p.id || null, revision: p.revision ?? null, status: p.status || "borrador",
                           pausedUntil: p.rewrite?.paused_until || 0, applied: !!p.rewrite?.applied,
                           createdAt: p.created_at || null });
    state.rewrites.clear(); state.rewriteStatus.clear();
    Object.entries(p.rewrite?.items || {}).forEach(([k, it]) => {
      const i = +k;
      state.rewriteStatus.set(i, it.status === "en_proceso" ? "pendiente" : it.status);
      if (it.rewritten) state.rewrites.set(i, { index: i, original: it.original, rewritten: it.rewritten,
                                                 changes: it.changes || [], accepted: it.accepted !== false });
    });
    if (p.report) {
      state.report = p.report; if (!state.report.text) state.report.text = d.text || "";
      $("#emptyState").classList.add("hidden"); render(state.report);
      if (pendingIndices().length) switchTab("rewrite");
    }
    if (state.status === "reescribiendo") state.status = "pausado";   // se cerró a mitad del proceso
    renderResumeControls(); saveLocal();
  }

  const periodoLabel = (v) => !v ? null : /^peri/i.test(String(v).trim()) ? String(v).trim() : `Periodo ${v}`;

  function draftSummary(p) {
    const items = Object.values(p.rewrite?.items || {});
    const done = items.filter((i) => i.status === "reescrito" || i.status === "sin_cambios").length;
    const when = p.saved_at || p.updated_at;
    const parts = [
      p.title || p.document?.title || "Sin título",
      when ? new Date(when).toLocaleString("es") : null,
      p.report ? `IA ${fmt(p.report.summary?.ai_probability)} %` : "sin auditar",
      items.length ? `${done}/${items.length} oraciones reescritas` : null,
      p.status === "pausado" ? "pausado por cuota" : null,
      p.context?.area_asignatura || p.context?.asignatura || p.area_asignatura,
      periodoLabel(p.context?.periodo || p.periodo),
    ];
    return parts.filter(Boolean).join(" · ");
  }

  async function checkForDraft() {
    let draft = null, source = "local";
    try { draft = JSON.parse(ls.get(DRAFT_KEY) || "null"); } catch (_) { draft = null; }
    if (!(draft && draft.status !== "finalizado" && (draft.report || draft.document?.text))) {
      draft = null;
      try {                                            // sin copia local: ¿hay algo en el servidor?
        const r = await apiGet("/api/v1/projects?limit=5");
        const meta = r.projects.find((x) => x.status !== "finalizado");
        if (meta) { draft = { ...meta, _remote: true }; source = "servidor"; }
      } catch (_) { /* sin servidor o sin permiso: se ignora */ }
    }
    if (!draft) return;
    $("#resumeMeta").textContent = `${draftSummary(draft)} · guardado en ${source === "local" ? "este navegador" : "el servidor"}`;
    $("#resumeBanner").classList.remove("hidden");
    draftDecisionPending = true;
    $("#resumeBtn").onclick = async () => {
      $("#resumeBtn").disabled = true;
      try {
        const p = draft._remote ? (await apiGet(`/api/v1/projects/load?id=${encodeURIComponent(draft.id)}`)).project : draft;
        draftDecisionPending = false;
        restoreProject(p);
        $("#resumeBanner").classList.add("hidden");
        toast(pendingIndices().length ? `Borrador restaurado: ${pendingIndices().length} oraciones pendientes de reescribir.` : "Borrador restaurado.");
      } catch (err) { toast(`No se pudo restaurar: ${err.message}`, 6000); }
      finally { $("#resumeBtn").disabled = false; }
    };
    $("#newProjectBtn").onclick = () => {
      if (!confirmNew()) return;
      draftDecisionPending = false;
      clearProject(); $("#resumeBanner").classList.add("hidden");
      toast("Proyecto nuevo iniciado." + (draft.id ? " El borrador anterior sigue disponible en el servidor." : ""));
    };
  }
  // Sin diálogos bloqueantes: segunda pulsación confirma.
  let confirmArmed = 0;
  function confirmNew() {
    if (Date.now() - confirmArmed < 4000) return true;
    confirmArmed = Date.now();
    toast("Pulsa de nuevo «Iniciar proyecto nuevo» para descartar el borrador local.", 4000);
    return false;
  }

  $("#saveDraftBtn").addEventListener("click", async () => {
    const b = $("#saveDraftBtn"); b.disabled = true;
    try { await saveServer({ silent: false }); } finally { b.disabled = false; }
  });
  // Guardado de emergencia al cerrar/recargar u ocultar la pestaña.
  window.addEventListener("pagehide", () => { if (hasContent()) saveLocal(); });
  document.addEventListener("visibilitychange", () => { if (document.hidden && hasContent()) saveLocal(); });

  /* ---------------- Motor de IA (Gemini / Claude) ---------------- */
  // Si el servidor no puede usar el motor elegido, responde llm.notice y usa Gemini:
  // se informa una sola vez y el selector vuelve a Gemini para las siguientes peticiones.
  let llmNoticeShown = false;
  function handleLlmNotice(llm) {
    if (!llm?.notice) return;
    if (!llmNoticeShown) { toast(llm.notice, 7000); llmNoticeShown = true; }
    if (llm.used === "gemini" && $("#llmProvider").value !== "gemini") { $("#llmProvider").value = "gemini"; scheduleSave(); }
  }
  $("#llmProvider").addEventListener("change", () => { llmNoticeShown = false; });

  /* ---------------- Institución ---------------- */
  const instSel = $("#institution");
  function updateInstNotes() {
    const o = instSel.selectedOptions[0];
    $("#instNotes").textContent = `${o.dataset.style} · ${o.dataset.notes} · Umbrales: IA ≤ ${o.dataset.ai}% · similitud ≤ ${o.dataset.sim}%`;
  }
  instSel.addEventListener("change", updateInstNotes); updateInstNotes();

  /* ---------------- Entrada ---------------- */
  const textInput = $("#textInput");
  const updateCount = () => $("#charCount").textContent = `${textInput.value.length.toLocaleString("es")} caracteres`;
  textInput.addEventListener("input", () => { updateCount(); scheduleSave(); });
  ["#docTitle", "#docAuthor"].forEach((sel) => $(sel).addEventListener("input", scheduleSave));
  $$("[data-ctx]").forEach((el) => el.addEventListener("input", scheduleSave));
  instSel.addEventListener("change", scheduleSave);
  $("#llmProvider").addEventListener("change", scheduleSave);
  $("#optFragments").addEventListener("input", (e) => $("#fragOut").value = e.target.value);

  const dz = $("#dropzone"), fileInput = $("#fileInput");
  ["dragenter", "dragover"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
  ["dragleave", "drop"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("drag"); }));
  dz.addEventListener("drop", (e) => e.dataTransfer.files[0] && loadFile(e.dataTransfer.files[0]));
  fileInput.addEventListener("change", () => fileInput.files[0] && loadFile(fileInput.files[0]));

  async function loadFile(file) {
    $("#fileName").textContent = `Leyendo ${file.name}…`;
    const fd = new FormData(); fd.append("file", file);
    try {
      const r = await api("/api/v1/extract", fd);
      textInput.value = r.text; updateCount(); $("#fileName").dataset.name = file.name;
      $("#fileName").textContent = `${file.name} · ${r.chars.toLocaleString("es")} caracteres${r.truncated ? " (truncado)" : ""}`;
      if (!$("#docTitle").value) $("#docTitle").value = file.name.replace(/\.[^.]+$/, "");
      saveLocal();
    } catch (err) { $("#fileName").textContent = ""; toast(err.message); }
  }

  /* ---------------- Auditoría ---------------- */
  let stepTimer;
  function showLoading(on) {
    $("#loading").classList.toggle("hidden", !on);
    $("#emptyState").classList.add("hidden");
    if (on) $("#report").classList.add("hidden");
    $("#auditBtn").disabled = on;
    clearInterval(stepTimer);
    const steps = $$(".steps li"); steps.forEach((s) => s.className = "");
    if (on) {
      let i = 0; steps[0].classList.add("active");
      stepTimer = setInterval(() => {
        if (i < steps.length - 1) { steps[i].className = "done"; steps[++i].className = "active"; }
      }, 1800);
    }
  }

  $("#auditBtn").addEventListener("click", async () => {
    const text = textInput.value.trim();
    if (text.length < 80) return toast("El texto debe tener al menos 80 caracteres.");
    if (draftDecisionPending) {             // auditar sin elegir = proyecto nuevo (el anterior queda en el servidor)
      draftDecisionPending = false; state.projectId = null; state.revision = null; state.createdAt = null;
      $("#resumeBanner").classList.add("hidden");
    }
    showLoading(true);
    try {
      const report = await api("/api/v1/audit", {
        text,
        institution: instSel.value,
        provider: $("#llmProvider").value,     // motor de IA: gemini | claude
        use_semantic: $("#optGemini").checked,
        check_plagiarism: $("#optPlag").checked,
        providers: $$("input[name=provider]:checked").map((c) => c.value),
        max_fragments: +$("#optFragments").value,
      });
      handleLlmNotice(report.llm);
      state.report = report; state.rewrites.clear(); state.applied = false;
      state.rewriteStatus = new Map(report.rewrite_targets.map((i) => [i, "pendiente"]));
      state.status = "auditado"; state.pausedUntil = 0;
      render(report); renderResumeControls();
      saveServer();                                    // local inmediato + servidor en segundo plano
    } catch (err) {
      toast(err.message, 6000);
      $("#emptyState").classList.remove("hidden");
    } finally { showLoading(false); }
  });

  /* ---------------- Render principal ---------------- */
  function render(r) {
    $("#report").classList.remove("hidden");
    const inst = r.institution, s = r.summary;
    const cls = s.verdict.startsWith("APROBADO") ? "ok" : s.verdict.startsWith("REVISIÓN") ? "bad" : "warn";
    const icon = { ok: "✓", warn: "!", bad: "✕" }[cls];
    $("#verdict").className = `verdict ${cls}`;
    $("#verdict").innerHTML = `<span style="font-size:1.4rem">${icon}</span><div>${esc(s.verdict)}
      <small>${esc(inst.name)} · ${esc(inst.citation_style)} · idioma detectado: ${r.language.toUpperCase()} · ${fmt(s.elapsed_ms / 1000)} s</small></div>`;

    gauge("#gAI", s.ai_probability, inst.ai_threshold);
    $("#gAIsub").textContent = `Umbral ${inst.ai_threshold}% · confianza ${fmt(r.ai.confidence * 100, 0)}%`;
    gauge("#gSIM", s.similarity_index, inst.similarity_threshold);
    $("#gSIMsub").textContent = r.plagiarism.available
      ? `Umbral ${inst.similarity_threshold}% · ${r.plagiarism.fragments_analyzed} fragmentos cotejados`
      : (r.plagiarism.error || "No evaluado");
    gauge("#gINT", s.integrity_score, 70, true);
    $("#gINTsub").textContent = "Combinación ponderada de IA y similitud";

    const m = r.ai.local.metrics;
    $("#stats").innerHTML = [
      [fmt(s.words, 0), "palabras"], [fmt(s.sentences, 0), "oraciones"],
      [fmt(s.flagged_sentences, 0), "fragmentos marcados"],
      [fmt(m.burstiness_cv, 2), "burstiness (CV)"],
      [fmt(m.perplexity_estimated, 0), "perplejidad estimada"],
      [fmt(m.marker_density_per100, 2), "muletillas / 100 palabras"],
    ].map(([v, l]) => `<div class="stat"><b>${v}</b><span>${l}</span></div>`).join("");

    renderMap(r); renderSources(r); renderMetrics(r); renderApi(r); renderRewrites();
    $("#srcCount").textContent = (r.plagiarism.sources || []).length;
    $("#sentenceDetail").classList.add("hidden");
  }

  function gauge(sel, value, threshold, invert = false) {
    const v = Math.max(0, Math.min(100, +value || 0));
    const R = 80, C = Math.PI * R, color = riskColor(v, threshold, invert);
    const ang = Math.PI * (1 - threshold / 100);
    const tx = 100 + (R + 12) * Math.cos(ang), ty = 100 - (R + 12) * Math.sin(ang);
    const ix = 100 + (R - 12) * Math.cos(ang), iy = 100 - (R - 12) * Math.sin(ang);
    $(sel).innerHTML = `<svg viewBox="0 0 200 120" role="img" aria-label="${fmt(v)} por ciento">
      <path d="M20 100 A80 80 0 0 1 180 100" fill="none" class="track" stroke-width="16" stroke-linecap="round"/>
      <path d="M20 100 A80 80 0 0 1 180 100" fill="none" stroke="${color}" stroke-width="16" stroke-linecap="round"
            stroke-dasharray="${C}" stroke-dashoffset="${C}" style="transition:stroke-dashoffset 1s ease">
        <animate attributeName="stroke-dashoffset" from="${C}" to="${C * (1 - v / 100)}" dur=".9s" fill="freeze"/></path>
      <line x1="${ix}" y1="${iy}" x2="${tx}" y2="${ty}" class="thr"/>
      <text x="100" y="96" text-anchor="middle" class="val">${fmt(v)}%</text>
      <text x="20" y="116" text-anchor="middle" class="lbl">0</text><text x="180" y="116" text-anchor="middle" class="lbl">100</text>
    </svg>`;
  }

  /* ---------------- Mapa de texto ---------------- */
  function renderMap(r) {
    const text = r.text, sents = r.sentences;
    let html = "", cursor = 0, para = [];
    const flush = () => { if (para.length) html += `<p>${para.join("")}</p>`; para = []; };
    let lastPara = sents.length ? sents[0].paragraph : 0;
    for (const s of sents) {
      if (s.paragraph !== lastPara) { flush(); lastPara = s.paragraph; cursor = s.start; }
      const gap = text.slice(cursor, s.start).replace(/\n+/g, " ");
      const rw = state.rewrites.get(s.index);
      const shown = state.applied && rw?.accepted ? rw.rewritten : s.text;
      const klass = state.applied && rw?.accepted ? "rewritten" : (s.flag !== "ok" ? s.flag : "");
      para.push(esc(gap) + `<span class="s ${klass}" data-i="${s.index}" tabindex="0" title="IA ${fmt(s.ai_score, 0)}%">${esc(shown)}</span>`);
      cursor = s.end;
    }
    flush();
    $("#docMap").innerHTML = html || "<p class='muted'>Sin contenido.</p>";
  }

  $("#docMap").addEventListener("click", (e) => {
    const el = e.target.closest(".s"); if (!el) return;
    $$(".s.sel").forEach((x) => x.classList.remove("sel")); el.classList.add("sel");
    showDetail(+el.dataset.i);
  });
  $("#docMap").addEventListener("keydown", (e) => { if (e.key === "Enter" && e.target.matches(".s")) e.target.click(); });

  function showDetail(i) {
    const s = state.report.sentences[i];
    const color = riskColor(s.ai_score, state.report.institution.ai_threshold);
    const pl = s.plagiarism;
    const rw = state.rewrites.get(i);
    const box = $("#sentenceDetail");
    box.classList.remove("hidden");
    box.innerHTML = `
      <h4>Oración ${i + 1}</h4>
      <blockquote>${esc(s.text)}</blockquote>
      <div class="muted tiny">Riesgo de IA</div>
      <div class="meter"><i style="width:${s.ai_score}%;background:${color}"></i></div>
      <b>${fmt(s.ai_score)}%</b>
      ${s.ai_reasons.length ? `<ul>${s.ai_reasons.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : `<p class="muted">Sin indicios relevantes.</p>`}
      ${pl ? `<div class="muted tiny">Similitud con literatura</div><b>${fmt(pl.similarity)}% · ${esc(pl.status)}</b>
        ${pl.top_match ? `<p class="tiny"><a href="${esc(pl.top_match.url)}" target="_blank" rel="noopener">${esc(pl.top_match.title)}</a><br>
          <span class="muted">${esc(pl.top_match.source)} · ${esc(pl.top_match.year || "s.f.")}</span></p>` : ""}` : ""}
      ${rw ? `<div class="muted tiny">Propuesta</div><blockquote>${esc(rw.rewritten)}</blockquote>` : ""}
      <button class="btn primary" id="rwOne">${rw ? "Generar otra versión" : "Reescribir esta oración"}</button>`;
    $("#rwOne").onclick = () => rewrite([i], true);
  }

  /* ---------------- Fuentes ---------------- */
  function renderSources(r) {
    const p = r.plagiarism;
    const errs = Object.entries(p.provider_errors || {});
    $("#providerErrors").innerHTML = errs.length
      ? `<p class="muted tiny">Fuentes no disponibles en esta consulta: ${errs.map(([k]) => esc(k)).join(", ")}.</p>` : "";
    if (!p.available) { $("#sourcesList").innerHTML = `<div class="card muted">${esc(p.error || "Cotejo desactivado.")}</div>`; return; }
    const src = p.sources || [];
    if (!src.length) {
      $("#sourcesList").innerHTML = `<div class="card">No se hallaron fuentes con similitud apreciable en ${p.fragments_analyzed} fragmentos cotejados.
        <p class="muted tiny">${esc(p.method)}</p></div>`; return;
    }
    $("#sourcesList").innerHTML = src.map((s) => {
      const c = riskColor(s.max_similarity, r.institution.similarity_threshold);
      return `<div class="card source">
        <div class="sim" style="background:color-mix(in srgb, ${c} 16%, transparent);color:${c}">${fmt(s.max_similarity, 0)}%</div>
        <div>
          <h5>${esc(s.title || "Sin título")}</h5>
          <div class="meta"><span class="tag">${esc(s.source)}</span>${esc((s.authors || []).filter(Boolean).join(", "))}
            ${s.year ? " · " + esc(s.year) : ""}${s.venue ? " · " + esc(s.venue) : ""}</div>
          ${s.url ? `<a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.url)}</a>` : ""}
          ${s.abstract ? `<p class="abs">${esc(s.abstract.slice(0, 280))}${s.abstract.length > 280 ? "…" : ""}</p>` : ""}
          <div class="meta">Coincide con oración(es): ${s.fragments.map((f) => `<a href="#" data-goto="${f}">${f + 1}</a>`).join(", ")}</div>
        </div></div>`;
    }).join("") + `<p class="muted tiny">${esc(p.method)}</p>`;
  }
  $("#sourcesList").addEventListener("click", (e) => {
    const a = e.target.closest("[data-goto]"); if (!a) return;
    e.preventDefault(); switchTab("map");
    const el = $(`.s[data-i="${a.dataset.goto}"]`); el?.scrollIntoView({ behavior: "smooth", block: "center" }); el?.click();
  });

  /* ---------------- Métricas ---------------- */
  function renderMetrics(r) {
    const sents = r.sentences.filter((s) => s.text.split(/\s+/).length > 0);
    const lens = r.ai.local.sentence_lengths;
    const max = Math.max(10, ...lens), W = 600, H = 170, bw = W / Math.max(lens.length, 1);
    const mean = r.ai.local.metrics.sentence_len_mean;
    const bars = lens.map((l, i) => {
      const h = (l / max) * (H - 20), s = sents[i] || { ai_score: 0 };
      const c = riskColor(s.ai_score, r.institution.ai_threshold);
      return `<rect x="${i * bw + 1}" y="${H - h}" width="${Math.max(bw - 2, 1)}" height="${h}" rx="2" fill="${c}" opacity=".85"><title>Oración ${i + 1}: ${l} palabras · IA ${fmt(s.ai_score, 0)}%</title></rect>`;
    }).join("");
    const my = H - (mean / max) * (H - 20);
    $("#burstChart").innerHTML = `<svg viewBox="0 0 ${W} ${H + 14}" preserveAspectRatio="none">
      ${bars}<line x1="0" x2="${W}" y1="${my}" y2="${my}" class="mean"/>
      <line x1="0" x2="${W}" y1="${H}" y2="${H}" class="axis"/></svg>
      <p class="muted tiny">Línea punteada: media de ${fmt(mean)} palabras por oración · desviación ${fmt(r.ai.local.metrics.sentence_len_std)}</p>`;

    const sub = r.ai.local.subscores;
    const labels = { burstiness: "Uniformidad de longitudes", perplexity_uniformity: "Perplejidad homogénea",
      robotic_markers: "Muletillas de LLM", structural_uniformity: "Uniformidad estructural" };
    $("#subscores").innerHTML = Object.entries(sub).map(([k, v]) => {
      const c = riskColor(v, 40);
      return `<div class="bar-row"><span>${labels[k] || k}</span><div class="meter"><i style="width:${v}%;background:${c}"></i></div><b>${fmt(v, 0)}</b></div>`;
    }).join("") + `<p class="muted tiny">Puntuación local: <b>${fmt(r.ai.local.score)}%</b> · B de Goh-Barabási: ${fmt(r.ai.local.metrics.burstiness_B, 2)} · MATTR: ${fmt(r.ai.local.metrics.lexical_diversity_mattr, 2)}
      ${r.ai.local.markers_found.length ? `<br>Muletillas detectadas: ${r.ai.local.markers_found.map(esc).join(", ")}` : ""}</p>`;

    const sem = r.ai.semantic;
    $("#semantic").innerHTML = sem.available
      ? `<p><b>${fmt(sem.ai_probability, 0)}%</b> probabilidad IA · predictibilidad ${fmt(sem.predictability, 0)}% · veredicto <b>${esc(sem.verdict)}</b> <span class="muted tiny">(${esc(sem.model)})</span></p>
         <p>${esc(sem.rationale)}</p>
         ${sem.robotic_phrases?.length ? `<ul>${sem.robotic_phrases.map((p) => `<li><b>«${esc(p.phrase)}»</b> — ${esc(p.reason)}</li>`).join("")}</ul>` : ""}
         <p class="muted tiny">${esc(r.ai.disclaimer)}</p>`
      : `<p class="muted">Capa semántica no disponible: ${esc(sem.error)}. El resultado se basa sólo en el motor matemático.</p>`;
  }

  /* ---------------- API ---------------- */
  function renderApi(r) {
    const origin = location.origin;
    $("#curlSample").textContent = `curl -X POST ${origin}/api/v1/audit \\
  -H "Content-Type: application/json" \\
  -H "X-API-Key: <SU_CLAVE>" \\
  -d '{"text": "…", "institution": "${r.institution.id}", "provider": "${$("#llmProvider").value}", "use_semantic": true, "check_plagiarism": true, "rewrite": false}'

# Con archivo:
curl -X POST ${origin}/api/v1/audit -H "X-API-Key: <SU_CLAVE>" \\
  -F "file=@tesis.docx" -F "institution=${r.institution.id}"`;
  }
  $("#downloadJson").addEventListener("click", () => {
    if (!state.report) return;
    const blob = new Blob([JSON.stringify(state.report, null, 2)], { type: "application/json" });
    const a = Object.assign(document.createElement("a"), { href: URL.createObjectURL(blob), download: "informe_auditoria.json" });
    a.click(); URL.revokeObjectURL(a.href);
  });

  /* ---------------- Reescritura por lotes ----------------
     El documento NUNCA se envía completo: cada petición lleva ≤ REWRITE_BATCH oraciones con
     su contexto local. Cada oración tiene un estado persistente:
       pendiente → en_proceso → reescrito | sin_cambios | fallido
     Tras cada lote se guarda en localStorage, así una recarga no pierde nada. */
  const REWRITE_BATCH = 4;            // debe ser ≤ REWRITE_MAX_PER_REQUEST del servidor (5)
  const MAX_ATTEMPTS = 4;             // intentos por lote ante errores temporales (5xx / red)
  const MAX_QUOTA_WAITS = 2;          // esperas por 429 antes de pausar el proceso
  const MAX_INLINE_WAIT = 60;         // s: si Google pide esperar más, se pausa directamente
  const MIN_PAUSE = 120;              // s: pausa mínima sugerida tras cuota agotada
  const PAUSE_BETWEEN_BATCHES = 700;  // ms: suaviza el consumo de tokens por minuto
  let rewriteCtrl = null;             // AbortController del proceso en curso

  class QuotaPause extends Error {
    constructor(retryAfter) { super("Cuota de uso excedida temporalmente"); this.name = "QuotaPause"; this.retryAfter = retryAfter; }
  }

  const pendingIndices = () => (state.report?.rewrite_targets || [])
    .filter((i) => ["pendiente", "fallido", "en_proceso", undefined].includes(state.rewriteStatus.get(i)));

  function setStatus(indices, st) { indices.forEach((i) => state.rewriteStatus.set(i, st)); }

  function buildSegments(indices) {
    const sents = state.report.sentences;
    return indices.map((i) => ({
      index: i,
      text: sents[i].text,
      before: i > 0 ? sents[i - 1].text.slice(-220) : "",
      after: i + 1 < sents.length ? sents[i + 1].text.slice(0, 220) : "",
    }));
  }

  /* Panel de progreso (se crea una sola vez bajo la barra de herramientas). */
  function progressUI() {
    let box = $("#rwProgress");
    if (!box) {
      box = document.createElement("div");
      box.id = "rwProgress"; box.className = "card rw-progress hidden";
      box.innerHTML = `<div class="rw-progress-head"><span id="rwProgLabel"></span><b id="rwProgPct">0%</b></div>
        <div class="meter" role="progressbar" aria-valuemin="0" aria-valuemax="100"><i id="rwProgBar" style="width:0%"></i></div>
        <div class="pause-notice hidden" id="rwPause" role="alert"></div>
        <div class="rw-progress-foot"><small class="muted" id="rwProgSub"></small>
        <button class="btn hidden" id="rwCancel" type="button">Cancelar</button></div>`;
      $("#tab-rewrite .toolbar").after(box);
      $("#rwCancel").addEventListener("click", () => rewriteCtrl?.abort());
    }
    return {
      show() { box.classList.remove("hidden"); $("#rwPause").classList.add("hidden"); $("#rwCancel").classList.remove("hidden"); },
      hide() { box.classList.add("hidden"); },
      set(done, total, label, sub = "") {
        const pct = total ? Math.round((done / total) * 100) : 0;
        $("#rwProgBar").style.width = pct + "%";
        box.querySelector("[role=progressbar]").setAttribute("aria-valuenow", pct);
        $("#rwProgPct").textContent = pct + "%";
        $("#rwProgLabel").textContent = label;
        $("#rwProgSub").textContent = sub;
      },
      pause(html) { box.classList.remove("hidden"); const n = $("#rwPause"); n.innerHTML = html; n.classList.remove("hidden"); },
    };
  }

  async function rewrite(indices, single = false) {
    if (!state.report) return;
    indices = [...new Set(indices)].filter((i) => state.report.sentences[i]);
    if (!indices.length) return toast("No hay fragmentos pendientes de reescribir.");
    if (rewriteCtrl) return toast("Ya hay una reescritura en curso.");

    const btn = single ? $("#rwOne") : $("#rewriteAllBtn");
    const label = btn?.textContent;
    if (btn) { btn.disabled = true; btn.textContent = "Reescribiendo…"; }
    $("#resumeRewriteBtn").disabled = true;
    if (!single) switchTab("rewrite");

    const batches = [];
    for (let i = 0; i < indices.length; i += REWRITE_BATCH) batches.push(indices.slice(i, i + REWRITE_BATCH));
    const total = indices.length, ui = progressUI(), engines = new Set(), warnings = new Set();
    let done = 0, produced = 0;
    rewriteCtrl = new AbortController();
    const { signal } = rewriteCtrl;
    setStatus(indices, "pendiente");
    state.status = "reescribiendo"; state.pausedUntil = 0;
    if (!single) ui.show();

    try {
      for (let b = 0; b < batches.length; b++) {
        const batch = batches[b];
        const head = `Reescribiendo lote ${b + 1} de ${batches.length}…`;
        ui.set(done, total, head, `${done}/${total} oraciones procesadas`);
        setStatus(batch, "en_proceso");

        let ok = false, quotaWaits = 0;
        for (let attempt = 1; attempt <= MAX_ATTEMPTS && !ok; attempt++) {
          try {
            const r = await api("/api/v1/rewrite", {
              segments: buildSegments(batch),
              institution: state.report.institution.id,
              mode: $("#rwMode").value,
              provider: $("#llmProvider").value,
            }, { signal });
            handleLlmNotice(r.llm);
            const got = new Set(r.rewrites.map((w) => w.index));
            r.rewrites.forEach((w) => state.rewrites.set(w.index, { ...w, accepted: true }));
            batch.forEach((i) => state.rewriteStatus.set(i, got.has(i) ? "reescrito" : "sin_cambios"));
            produced += r.rewrites.length;
            engines.add(r.engine); if (r.warning) warnings.add(r.warning); if (r.note) warnings.add(r.note);
            renderRewrites();
            saveLocal();                                // avance asegurado tras cada lote
            ok = true;
          } catch (err) {
            if (err.name === "AbortError") throw err;
            if (err.status === 429) {
              // Cuota agotada: se espera poco; si persiste o Google pide mucho tiempo, se PAUSA.
              const wait = Math.max(err.retryAfter || 10, 3);
              if (++quotaWaits > MAX_QUOTA_WAITS || wait > MAX_INLINE_WAIT) throw new QuotaPause(wait);
              attempt--;                                // las esperas por cuota no consumen intentos
              for (let t = Math.ceil(wait); t > 0; t--) {
                ui.set(done, total, head, `Cuota de uso excedida temporalmente: reintentando en ${t} s (espera ${quotaWaits}/${MAX_QUOTA_WAITS})`);
                await sleep(1000, signal);
              }
              continue;
            }
            if (!err.retryable || attempt === MAX_ATTEMPTS) {
              console.warn(`Lote ${b + 1} falló:`, err.message);
              warnings.add(`Lote ${b + 1}: ${err.message}`);
              break;
            }
            const wait = 2 ** attempt * 1.5;             // 5xx / red: backoff exponencial
            for (let t = Math.ceil(wait); t > 0; t--) {
              ui.set(done, total, head, `Error temporal (${err.status || "red"}): reintentando en ${t} s`);
              await sleep(1000, signal);
            }
          }
        }
        if (!ok) { setStatus(batch, "fallido"); saveLocal(); }
        done += batch.length;
        ui.set(done, total, `Reescribiendo lote ${Math.min(b + 2, batches.length)} de ${batches.length}…`,
               `${done}/${total} oraciones procesadas · ${produced} propuestas`);
        if (b < batches.length - 1) await sleep(PAUSE_BETWEEN_BATCHES, signal);
      }
      state.status = "auditado";
      finishRewrite(ui, { total, produced, engines, warnings, single, indices });
    } catch (err) {
      setStatus(indices.filter((i) => state.rewriteStatus.get(i) === "en_proceso"), "pendiente");
      if (err.name === "QuotaPause") {
        pauseForQuota(ui, err.retryAfter, { done, total, produced });
      } else if (err.name === "AbortError") {
        state.status = "auditado";
        ui.set(done, total, "Reescritura cancelada", `${produced} propuestas conservadas`);
        toast("Reescritura cancelada. Las propuestas ya generadas se conservan.");
      } else { state.status = "auditado"; toast(err.message, 6000); }
      saveLocal();
    } finally {
      rewriteCtrl = null;
      $("#rwCancel")?.classList.add("hidden");
      if (btn && document.body.contains(btn)) { btn.disabled = false; btn.textContent = label; }
      $("#resumeRewriteBtn").disabled = false;
      renderResumeControls();
      if (!single) saveServer();                         // copia en servidor al terminar o pausar
    }
  }

  function pauseForQuota(ui, retryAfter, { done, total, produced }) {
    const secs = Math.max(retryAfter || 0, MIN_PAUSE);
    state.status = "pausado";
    state.pausedUntil = Date.now() + secs * 1000;
    showPauseNotice(ui, done, total, produced);
    toast("Cuota de uso excedida temporalmente: reescritura pausada y avance guardado.", 6000);
  }

  let pauseTimer = null;
  function tickPauseCountdown() {
    clearInterval(pauseTimer);
    const paint = () => {
      const el = $("#pauseCountdown"); const left = Math.ceil((state.pausedUntil - Date.now()) / 1000);
      if (!el || left <= 0 || state.status !== "pausado") {
        if (el) el.textContent = "Ya puedes reanudar.";
        return clearInterval(pauseTimer);
      }
      el.textContent = `Reanudación sugerida en ${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}.`;
    };
    paint(); pauseTimer = setInterval(paint, 1000);
  }

  /** Botón "Reanudar reescritura pendiente" de la barra y aviso de pausa tras restaurar. */
  function renderResumeControls() {
    const b = $("#resumeRewriteBtn"); if (!b) return;
    const n = pendingIndices().length;
    const failed = (state.report?.rewrite_targets || []).filter((i) => state.rewriteStatus.get(i) === "fallido").length;
    b.classList.toggle("hidden", !(state.report && n && state.rewriteStatus.size));
    b.textContent = `Reanudar reescritura pendiente (${n})`;
    b.title = failed ? `${failed} fallidas + ${n - failed} pendientes` : `${n} oraciones pendientes`;
    if (state.status === "pausado" && n && !rewriteCtrl) {
      const ui = progressUI();
      const total = state.report.rewrite_targets.length;
      showPauseNotice(ui, total - n, total);
    }
  }
  /** Aviso de pausa (tras cuota agotada o al restaurar un proyecto pausado). */
  function showPauseNotice(ui, done, total, produced = null) {
    const pending = pendingIndices().length;
    ui.set(done, total, "Reescritura en pausa",
           `${produced ?? state.rewrites.size} propuestas conservadas · ${pending} oraciones pendientes`);
    ui.pause(`<b>Cuota de uso pausada temporalmente.</b>
      Tu avance se ha guardado automáticamente. Puedes reanudar la reescritura de las oraciones restantes
      en unos minutos o descargar el informe parcial.
      <span class="muted" id="pauseCountdown"></span>
      <div class="resume-actions" style="margin-top:.5rem">
        <button class="btn primary" type="button" data-act="resume">Reanudar reescritura pendiente (${pending})</button>
        <button class="btn accent" type="button" data-act="partial">Descargar informe parcial</button>
      </div>`);
    $("#rwPause").querySelector("[data-act=resume]").onclick = () => rewrite(pendingIndices());
    $("#rwPause").querySelector("[data-act=partial]").onclick = () => exportDocx();
    tickPauseCountdown();
  }

  function finishRewrite(ui, { total, produced, engines, warnings, single, indices }) {
    const failed = indices.filter((i) => state.rewriteStatus.get(i) === "fallido").length;
    ui.set(total, total, failed ? `Completado con ${failed} oración(es) fallidas` : "Reescritura completada",
           `${produced} propuestas generadas de ${total} oraciones`);
    $("#rwEngine").textContent = `Motor: ${[...engines].join(", ") || "—"}${warnings.size ? " · " + [...warnings].join(" · ") : ""}`;
    renderRewrites(); saveLocal();
    if (single) { showDetail(indices[0]); if (!produced) toast("El motor no propuso cambios para esta oración."); return; }
    toast(failed ? `${produced} propuestas generadas; ${failed} oraciones fallaron: usa «Reanudar reescritura pendiente».`
                 : `${produced} propuesta(s) generadas.`, 5000);
    if (!failed) setTimeout(() => !rewriteCtrl && state.status !== "pausado" && ui.hide(), 4000);
  }

  $("#rewriteAllBtn").addEventListener("click", () => {
    const pending = pendingIndices();
    rewrite(pending.length ? pending : state.report?.rewrite_targets || []);
  });
  $("#resumeRewriteBtn").addEventListener("click", () => rewrite(pendingIndices()));

  function renderRewrites() {
    const items = [...state.rewrites.values()].sort((a, b) => a.index - b.index);
    $("#rwCount").textContent = items.length;
    $("#applyBtn").disabled = !items.length;
    $("#rewriteList").innerHTML = items.length ? items.map((w) => `
      <div class="card rw" data-i="${w.index}">
        <div class="orig">${esc(w.original)}</div>
        <div class="new" contenteditable="true" spellcheck="true" aria-label="Versión reescrita (editable)">${esc(w.rewritten)}</div>
        <label class="switch small"><input type="checkbox" class="acc" ${w.accepted ? "checked" : ""}><span></span></label>
        ${w.changes?.length ? `<div class="changes">Oración ${w.index + 1} · ${w.changes.map(esc).join(" · ")}</div>` : `<div class="changes">Oración ${w.index + 1}</div>`}
      </div>`).join("")
      : `<div class="card muted">Pulsa <b>Reescribir fragmentos marcados</b> para generar propuestas para las ${state.report?.rewrite_targets?.length || 0} oraciones con riesgo.</div>`;
    updatePreview();
  }

  $("#rewriteList").addEventListener("input", (e) => {
    const card = e.target.closest(".rw"); if (!card) return;
    const w = state.rewrites.get(+card.dataset.i);
    if (e.target.matches(".new")) w.rewritten = e.target.innerText.trim();
    updatePreview(); scheduleSave();
  });
  $("#rewriteList").addEventListener("change", (e) => {
    const card = e.target.closest(".rw"); if (!card || !e.target.matches(".acc")) return;
    state.rewrites.get(+card.dataset.i).accepted = e.target.checked; updatePreview(); scheduleSave();
  });

  function finalText() {
    const r = state.report; if (!r) return "";
    let out = r.text;
    [...r.sentences].sort((a, b) => b.start - a.start).forEach((s) => {
      const w = state.rewrites.get(s.index);
      if (w?.accepted && w.rewritten) out = out.slice(0, s.start) + w.rewritten + out.slice(s.end);
    });
    return out;
  }
  function updatePreview() { $("#finalPreview").textContent = finalText(); }

  $("#applyBtn").addEventListener("click", () => {
    state.applied = true; renderMap(state.report); switchTab("map"); saveLocal();
    toast("Cambios aplicados al mapa de texto. Descarga el .docx para obtener el documento final.");
  });

  /* ---------------- Exportación DOCX ---------------- */
  $("#exportBtn").addEventListener("click", () => exportDocx());
  async function exportDocx() {
    if (!state.report) return;
    const r = state.report, accepted = [...state.rewrites.values()].filter((w) => w.accepted);
    const btn = $("#exportBtn"); btn.disabled = true;
    try {
      const res = await api("/api/v1/export", {
        text: finalText(), institution: r.institution.id,
        title: $("#docTitle").value, author: $("#docAuthor").value,
        include_report: $("#includeReport").checked,
        report: { ai: { ai_probability: r.ai.ai_probability, local: { metrics: r.ai.local.metrics }, disclaimer: r.ai.disclaimer },
                  plagiarism: { similarity_index: r.plagiarism.similarity_index, sources: (r.plagiarism.sources || []).slice(0, 15) } },
        rewrites: accepted.map(({ original, rewritten }) => ({ original, rewritten })),
        filename: ($("#docTitle").value || "documento_corregido").replace(/[^\w\-áéíóúñÁÉÍÓÚÑ ]+/g, "").trim().replace(/\s+/g, "_"),
      }, { raw: true });
      const blob = await res.blob();
      const name = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(res.headers.get("Content-Disposition") || "")?.[1] || "documento_corregido.docx";
      const a = Object.assign(document.createElement("a"), { href: URL.createObjectURL(blob), download: decodeURIComponent(name) });
      document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(a.href);
      // Sin oraciones pendientes, exportar cierra el proyecto (ya no se ofrece reanudarlo).
      if (!pendingIndices().length && !rewriteCtrl) state.status = "finalizado";
      saveServer();
    } catch (err) { toast(err.message); } finally { btn.disabled = false; }
  }

  /* ---------------- Pestañas ---------------- */
  function switchTab(name) {
    $$(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
    $$(".tabpane").forEach((p) => p.classList.toggle("active", p.id === `tab-${name}`));
  }
  $$(".tabs button").forEach((b) => b.addEventListener("click", () => switchTab(b.dataset.tab)));

  /* ---------------- Arranque: ¿hay un borrador que reanudar? ---------------- */
  checkForDraft();
})();
