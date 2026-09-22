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
    report: null,        // última respuesta de /api/v1/audit
    rewrites: new Map(), // index -> {original, rewritten, changes, accepted}
    applied: false,
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

  async function api(path, body, { raw = false } = {}) {
    const opts = body instanceof FormData
      ? { method: "POST", body }
      : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
    const res = await fetch(path, { credentials: "same-origin", ...opts });
    if (!res.ok) {
      let msg = `Error ${res.status}`;
      try { const j = await res.json(); msg = j.error || msg; } catch (_) {}
      throw new Error(msg);
    }
    return raw ? res : res.json();
  }

  function riskColor(v, thr, invert = false) {
    const css = getComputedStyle(root);
    if (invert) v = 100 - v, thr = 100 - thr;
    if (v <= thr) return css.getPropertyValue("--ok").trim();
    if (v <= Math.max(thr * 2, 60)) return css.getPropertyValue("--warn").trim();
    return css.getPropertyValue("--danger").trim();
  }

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
  textInput.addEventListener("input", updateCount);
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
      textInput.value = r.text; updateCount();
      $("#fileName").textContent = `${file.name} · ${r.chars.toLocaleString("es")} caracteres${r.truncated ? " (truncado)" : ""}`;
      if (!$("#docTitle").value) $("#docTitle").value = file.name.replace(/\.[^.]+$/, "");
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
    showLoading(true);
    try {
      const report = await api("/api/v1/audit", {
        text,
        institution: instSel.value,
        use_gemini: $("#optGemini").checked,
        check_plagiarism: $("#optPlag").checked,
        providers: $$("input[name=provider]:checked").map((c) => c.value),
        max_fragments: +$("#optFragments").value,
      });
      state.report = report; state.rewrites.clear(); state.applied = false;
      render(report);
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
  -d '{"text": "…", "institution": "${r.institution.id}", "use_gemini": true, "check_plagiarism": true, "rewrite": false}'

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

  /* ---------------- Reescritura ---------------- */
  async function rewrite(indices, single = false) {
    if (!state.report) return;
    if (!indices.length) return toast("No hay fragmentos marcados para reescribir.");
    const btn = single ? $("#rwOne") : $("#rewriteAllBtn");
    const label = btn.textContent; btn.disabled = true; btn.textContent = "Reescribiendo…";
    try {
      const r = await api("/api/v1/rewrite", {
        text: state.report.text, institution: state.report.institution.id,
        indices, mode: $("#rwMode").value,
      });
      r.rewrites.forEach((w) => state.rewrites.set(w.index, { ...w, accepted: true }));
      $("#rwEngine").textContent = `Motor: ${r.engine}${r.warning ? " · " + r.warning : ""}${r.note ? " · " + r.note : ""}`;
      if (!r.rewrites.length) toast("El motor no propuso cambios para esos fragmentos.");
      renderRewrites();
      if (single) showDetail(indices[0]); else switchTab("rewrite");
      toast(`${r.rewrites.length} propuesta(s) generadas.`);
    } catch (err) { toast(err.message, 6000); }
    finally { btn.disabled = false; btn.textContent = label; }
  }

  $("#rewriteAllBtn").addEventListener("click", () => rewrite(state.report?.rewrite_targets || []));

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
    updatePreview();
  });
  $("#rewriteList").addEventListener("change", (e) => {
    const card = e.target.closest(".rw"); if (!card || !e.target.matches(".acc")) return;
    state.rewrites.get(+card.dataset.i).accepted = e.target.checked; updatePreview();
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
    state.applied = true; renderMap(state.report); switchTab("map");
    toast("Cambios aplicados al mapa de texto. Descarga el .docx para obtener el documento final.");
  });

  /* ---------------- Exportación DOCX ---------------- */
  $("#exportBtn").addEventListener("click", async () => {
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
    } catch (err) { toast(err.message); } finally { btn.disabled = false; }
  });

  /* ---------------- Pestañas ---------------- */
  function switchTab(name) {
    $$(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
    $$(".tabpane").forEach((p) => p.classList.toggle("active", p.id === `tab-${name}`));
  }
  $$(".tabs button").forEach((b) => b.addEventListener("click", () => switchTab(b.dataset.tab)));
})();
