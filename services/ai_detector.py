"""
Motor dual de detección de texto generado por IA.

1) Capa MATEMÁTICA (local, determinista, sin red)
   - Burstiness: coeficiente de variación (CV) de la longitud de oraciones y
     el índice de Goh-Barabási  B = (σ-μ)/(σ+μ).  El texto humano alterna
     oraciones cortas y largas (B alto); los LLM tienden a la uniformidad.
   - Perplejidad estimada: modelo de lenguaje bigrama interpolado (Jelinek-
     Mercer) entrenado sobre el propio documento con validación *leave-one-
     sentence-out*. Se calcula la perplejidad de cada oración y su dispersión:
     una dispersión baja indica predictibilidad homogénea, rasgo típico de IA.
   - Densidad de muletillas/fórmulas propias de LLM (ES/EN).
   - Uniformidad estructural: repetición de arranques de oración y pobreza de
     puntuación expresiva.

2) Capa SEMÁNTICA (Gemini 2.5)
   - Evalúa predictibilidad del discurso, muletillas robóticas, genericidad y
     ausencia de voz autoral; marca oraciones concretas.

Ambas capas se fusionan en una probabilidad final (0-100) con un nivel de
confianza que depende de la longitud del texto. Ningún detector de IA es
infalible: el resultado es un indicio probabilístico, no una prueba.
"""
from __future__ import annotations

import logging
import math
import statistics
from collections import Counter

from .gemini_client import GeminiClient, GeminiError
from .institutions import Institution
from .text_utils import (STOPWORDS_EN, STOPWORDS_ES, Sentence, clamp, find_markers,
                         tokenize)

log = logging.getLogger(__name__)

LOCAL_WEIGHT = 0.40          # peso de la capa matemática cuando hay Gemini
SEMANTIC_WEIGHT = 0.60
MAX_SENTENCES_TO_LLM = 160


# --------------------------------------------------------------------------- #
#  Capa matemática
# --------------------------------------------------------------------------- #
def _burstiness(lengths: list[int]) -> dict:
    if len(lengths) < 2:
        return {"mean": float(lengths[0]) if lengths else 0.0, "std": 0.0, "cv": 0.0, "B": -1.0}
    mu = statistics.fmean(lengths)
    sd = statistics.pstdev(lengths)
    cv = sd / mu if mu else 0.0
    b = (sd - mu) / (sd + mu) if (sd + mu) else -1.0
    return {"mean": round(mu, 2), "std": round(sd, 2), "cv": round(cv, 3), "B": round(b, 3)}


def _sentence_perplexities(token_lists: list[list[str]], lam: float = 0.7, k: float = 0.1) -> list[float]:
    """
    Perplejidad por oración con un bigrama interpolado entrenado sobre el resto
    del documento (leave-one-out). Devuelve lista paralela a ``token_lists``.
    """
    uni: Counter = Counter()
    bi: Counter = Counter()
    for toks in token_lists:
        seq = ["<s>"] + toks
        uni.update(seq)
        bi.update(zip(seq, seq[1:]))
    vocab = len(uni) + 1
    total = sum(uni.values())

    ppls: list[float] = []
    for toks in token_lists:
        if not toks:
            ppls.append(0.0)
            continue
        seq = ["<s>"] + toks
        own_uni = Counter(seq)
        own_bi = Counter(zip(seq, seq[1:]))
        n_total = total - len(seq)
        log_prob = 0.0
        for prev, word in zip(seq, seq[1:]):
            c_w = uni[word] - own_uni[word]
            c_prev = uni[prev] - own_uni[prev]
            c_bi = bi[(prev, word)] - own_bi[(prev, word)]
            p_uni = (c_w + k) / (n_total + k * vocab) if n_total > 0 else 1 / vocab
            p_bi = (c_bi + k) / (c_prev + k * vocab) if c_prev > 0 else p_uni
            p = lam * p_bi + (1 - lam) * p_uni
            log_prob += math.log2(max(p, 1e-12))
        ppls.append(2 ** (-log_prob / len(toks)))
    return ppls


def _mattr(words: list[str], window: int = 50) -> float:
    """Moving-Average Type-Token Ratio (diversidad léxica robusta a la longitud)."""
    if len(words) < window:
        return len(set(words)) / len(words) if words else 0.0
    ratios = [len(set(words[i:i + window])) / window for i in range(0, len(words) - window + 1, 5)]
    return statistics.fmean(ratios)


def local_analysis(sentences: list[Sentence], text: str, lang: str) -> dict:
    token_lists = [s.words for s in sentences]
    lengths = [len(t) for t in token_lists if t]
    all_words = [w for t in token_lists for w in t]
    n_words = len(all_words)

    burst = _burstiness(lengths)
    ppls = _sentence_perplexities(token_lists)
    valid_ppl = [p for p, t in zip(ppls, token_lists) if len(t) >= 4]
    log_ppl = [math.log2(p) for p in valid_ppl if p > 0]
    ppl_mean = statistics.fmean(valid_ppl) if valid_ppl else 0.0
    ppl_cv = (statistics.pstdev(log_ppl) / statistics.fmean(log_ppl)) if len(log_ppl) > 1 and statistics.fmean(log_ppl) else 0.0

    markers = find_markers(text)
    marker_hits = sum(text.lower().count(m) for m in markers)
    marker_density = marker_hits / max(n_words, 1) * 100  # por cada 100 palabras

    starts = [t[0] for t in token_lists if t]
    start_rep = 1 - (len(set(starts)) / len(starts)) if starts else 0.0
    expressive_punct = sum(text.count(c) for c in ";:—()¿?¡!…\"«»") / max(len(sentences), 1)
    stop = STOPWORDS_ES if lang == "es" else STOPWORDS_EN
    function_ratio = sum(w in stop for w in all_words) / max(n_words, 1)
    mattr = _mattr(all_words)

    # --- Sub-puntuaciones normalizadas (1 = rasgo típico de IA) -------------
    s_burst = clamp((0.62 - burst["cv"]) / 0.37)            # CV ≤ 0.25 → 1 ; ≥ 0.62 → 0
    s_ppl = clamp((0.16 - ppl_cv) / 0.10)                   # dispersión log-PPL baja → IA
    s_mark = clamp(marker_density / 1.6)
    s_struct = clamp(0.6 * clamp(start_rep / 0.35) + 0.4 * clamp((0.6 - expressive_punct) / 0.6))

    # El modelo bigrama autoentrenado sólo es informativo con suficientes
    # oraciones; en textos cortos su peso se redistribuye al resto de señales.
    w_ppl = 0.22 * clamp((len(lengths) - 10) / 25)
    rest = 1 - w_ppl
    score = (w_ppl * s_ppl + rest * (0.44 * s_burst + 0.36 * s_mark + 0.20 * s_struct))

    # Confianza: con pocos datos las métricas estadísticas no son fiables.
    confidence = clamp((len(lengths) - 3) / 22) * clamp(n_words / 250)
    # Con poca evidencia se contrae hacia 50 (incertidumbre).
    score = 0.5 + (score - 0.5) * (0.35 + 0.65 * confidence)

    per_sentence = _local_sentence_scores(sentences, token_lists, ppls, burst["mean"], burst["std"])

    return {
        "score": round(score * 100, 1),
        "confidence": round(confidence, 2),
        "metrics": {
            "words": n_words,
            "sentences": len(sentences),
            "burstiness_cv": burst["cv"],
            "burstiness_B": burst["B"],
            "sentence_len_mean": burst["mean"],
            "sentence_len_std": burst["std"],
            "perplexity_estimated": round(ppl_mean, 1),
            "perplexity_dispersion": round(ppl_cv, 3),
            "marker_density_per100": round(marker_density, 2),
            "sentence_start_repetition": round(start_rep, 3),
            "expressive_punctuation": round(expressive_punct, 2),
            "function_word_ratio": round(function_ratio, 3),
            "lexical_diversity_mattr": round(mattr, 3),
        },
        "subscores": {
            "burstiness": round(s_burst * 100, 1),
            "perplexity_uniformity": round(s_ppl * 100, 1),
            "robotic_markers": round(s_mark * 100, 1),
            "structural_uniformity": round(s_struct * 100, 1),
        },
        "markers_found": markers,
        "sentence_lengths": lengths,
        "sentence_perplexities": [round(p, 1) for p in ppls],
        "per_sentence": per_sentence,
    }


def _local_sentence_scores(sentences, token_lists, ppls, mean_len, std_len) -> list[dict]:
    valid = [p for p, t in zip(ppls, token_lists) if len(t) >= 4]
    p_mu = statistics.fmean(valid) if valid else 0
    p_sd = statistics.pstdev(valid) if len(valid) > 1 else 1
    out = []
    for s, toks, ppl in zip(sentences, token_lists, ppls):
        reasons = []
        score = 0.18
        markers = find_markers(s.text)
        if markers:
            score += min(0.5, 0.22 * len(markers))
            reasons.append("Muletillas: " + ", ".join(markers[:4]))
        if toks and std_len and abs(len(toks) - mean_len) < 0.35 * std_len and len(toks) > 12:
            score += 0.12
            reasons.append("Longitud muy próxima al promedio (baja variabilidad)")
        if len(toks) >= 4 and p_sd and (ppl - p_mu) / p_sd < -0.6:
            score += 0.14
            reasons.append("Baja perplejidad relativa (alta predictibilidad)")
        if len(toks) > 35:
            score += 0.06
            reasons.append("Oración extensa con estructura enumerativa")
        out.append({"index": s.index, "score": round(clamp(score) * 100, 1), "reasons": reasons})
    return out


# --------------------------------------------------------------------------- #
#  Capa semántica (Gemini)
# --------------------------------------------------------------------------- #
_SYSTEM_SEMANTIC = (
    "Eres un lingüista forense experto en detección de texto generado por modelos de lenguaje "
    "(ChatGPT, Gemini, Claude, Llama). Evalúas con rigor y sin sesgos: los textos académicos "
    "humanos también pueden ser formales. Considera: predictibilidad léxica, fórmulas de "
    "transición genéricas, simetría estructural, ausencia de voz autoral, afirmaciones vagas sin "
    "datos concretos, enumeraciones de tres elementos repetitivas, cierres moralizantes y "
    "hedging excesivo. Responde SOLO con JSON válido."
)


def _build_semantic_prompt(sentences: list[Sentence], institution: Institution) -> tuple[str, list[int]]:
    idx = list(range(len(sentences)))
    if len(idx) > MAX_SENTENCES_TO_LLM:  # muestreo uniforme para documentos largos
        step = len(idx) / MAX_SENTENCES_TO_LLM
        idx = sorted({int(i * step) for i in range(MAX_SENTENCES_TO_LLM)})
    numbered = "\n".join(f"[{i}] {sentences[i].text}" for i in idx)
    prompt = f"""Contexto institucional: {institution.name} ({institution.citation_style}).

Analiza las oraciones numeradas y devuelve exactamente este JSON:
{{
  "ai_probability": <entero 0-100, probabilidad global de generación por IA>,
  "predictability": <entero 0-100, cuán predecible/genérico es el lenguaje>,
  "verdict": "humano" | "mixto" | "ia",
  "robotic_phrases": [{{"phrase": "<fragmento literal>", "reason": "<breve>"}}],
  "flagged_sentences": [{{"index": <número entre corchetes>, "score": <0-100>, "reason": "<breve, en español>"}}],
  "rationale": "<2-4 oraciones en español explicando el dictamen>"
}}
Marca solo oraciones con indicios reales (score >= 50). Máximo 12 robotic_phrases.

TEXTO:
{numbered}"""
    return prompt, idx


def semantic_analysis(sentences: list[Sentence], institution: Institution,
                      client: GeminiClient) -> dict:
    if not client.enabled:
        return {"available": False, "error": "Gemini no configurado (GEMINI_API_KEY)"}
    if not sentences:
        return {"available": False, "error": "Texto vacío"}
    prompt, _ = _build_semantic_prompt(sentences, institution)
    try:
        data = client.generate_json(_SYSTEM_SEMANTIC, prompt, temperature=0.1)
    except GeminiError as exc:
        return {"available": False, "error": str(exc)}
    if not isinstance(data, dict):
        return {"available": False, "error": "Formato inesperado"}

    flagged = []
    for item in data.get("flagged_sentences", []) or []:
        try:
            i = int(item.get("index"))
        except (TypeError, ValueError):
            continue
        if 0 <= i < len(sentences):
            flagged.append({"index": i, "score": int(clamp(float(item.get("score", 60)), 0, 100)),
                            "reason": str(item.get("reason", ""))[:240]})
    return {
        "available": True,
        "model": client.model,
        "ai_probability": int(clamp(float(data.get("ai_probability", 50)), 0, 100)),
        "predictability": int(clamp(float(data.get("predictability", 50)), 0, 100)),
        "verdict": str(data.get("verdict", "mixto")),
        "robotic_phrases": (data.get("robotic_phrases") or [])[:12],
        "flagged_sentences": flagged,
        "rationale": str(data.get("rationale", ""))[:1200],
    }


# --------------------------------------------------------------------------- #
#  Fusión
# --------------------------------------------------------------------------- #
def detect_ai(sentences: list[Sentence], text: str, lang: str, institution: Institution,
              client: GeminiClient | None = None, use_semantic: bool = True) -> dict:
    local = local_analysis(sentences, text, lang)
    semantic = (semantic_analysis(sentences, institution, client)
                if (use_semantic and client) else {"available": False, "error": "Desactivado"})

    if semantic.get("available"):
        final = LOCAL_WEIGHT * local["score"] + SEMANTIC_WEIGHT * semantic["ai_probability"]
        confidence = clamp(0.35 + 0.65 * local["confidence"] + 0.15)
    else:
        final = local["score"]
        confidence = local["confidence"] * 0.8

    sem_map = {f["index"]: f for f in semantic.get("flagged_sentences", [])}
    per_sentence = []
    for item in local["per_sentence"]:
        sem = sem_map.get(item["index"])
        score = item["score"]
        reasons = list(item["reasons"])
        if sem:
            score = 0.35 * score + 0.65 * sem["score"]
            if sem["reason"]:
                reasons.insert(0, "IA semántica: " + sem["reason"])
        elif semantic.get("available"):
            score *= 0.8
        per_sentence.append({"index": item["index"], "score": round(score, 1), "reasons": reasons})

    final = round(clamp(final, 0, 100), 1)
    return {
        "ai_probability": final,
        "confidence": round(confidence, 2),
        "level": "alto" if final >= 60 else "medio" if final >= institution.ai_threshold else "bajo",
        "within_threshold": final <= institution.ai_threshold,
        "local": {k: v for k, v in local.items() if k != "per_sentence"},
        "semantic": semantic,
        "per_sentence": per_sentence,
        "disclaimer": ("La detección de IA es probabilística. Úsese como indicio para revisión "
                       "humana, nunca como prueba única de mala conducta académica."),
    }
