"""
Motor de reescritura / parafraseo con estilo institucional.

Modo principal: motor de IA seleccionado (Gemini o Claude) con instrucciones de estilo de la institución.
Modo de respaldo (sin clave): reglas locales — sustituye muletillas, elimina
fórmulas vacías y divide oraciones excesivamente largas.

Principios editoriales que se imponen al modelo:
    * conservar el significado, los datos, las cifras y las citas (Autor, año) / [n];
    * no inventar referencias ni hechos;
    * variar la longitud y la estructura de las oraciones (voz autoral natural);
    * aplicar la norma de la institución seleccionada.

Uso responsable: la herramienta mejora la redacción de ideas propias. Las
ideas tomadas de otros autores deben seguir citándose aunque se parafraseen.
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass

from .llm import LLMAuthError, LLMError, LLMRateLimitError, LLMTimeoutError
from .institutions import Institution
from .text_utils import TRANSITIONS_EN, TRANSITIONS_ES, Sentence

log = logging.getLogger(__name__)

MODES = {
    "academico": "Registro académico formal, preciso y natural; prioriza claridad y rigor.",
    "fluido": ("Prosa humana y fluida: alterna oraciones breves y largas, usa conectores variados "
               "y poco previsibles, elimina fórmulas genéricas y aporta una voz autoral clara."),
    "conciso": "Reduce palabras sin perder contenido; elimina redundancias y rodeos.",
}

_SYSTEM = (
    "Eres un corrector de estilo académico senior y editor de tesis. Reescribes fragmentos para "
    "que suenen escritos por una persona experta, con variabilidad sintáctica natural, "
    "cumpliendo la norma institucional indicada. REGLAS INQUEBRANTABLES: conserva el significado; "
    "no agregues datos, ejemplos ni referencias que no estén en el original; conserva literalmente "
    "cifras, fórmulas, nombres propios y citas como (Apellido, 2020), [3] o (4); escribe en el "
    "mismo idioma del original. Responde SOLO con JSON válido."
)

# Oraciones por llamada al motor de IA. Lotes pequeños => cada llamada tarda pocos segundos
# y consume pocos tokens por minuto (clave en el plan gratuito de Gemini y de Render).
BATCH_SIZE = max(1, int(os.getenv("REWRITE_BATCH_SIZE", "4")))
CONTEXT_CHARS = 220          # contexto local antes/después de cada oración
MAX_SEGMENT_CHARS = 1500     # una "oración" más larga que esto se recorta al validar


@dataclass
class Segment:
    """Unidad mínima de reescritura: la oración y su contexto local inmediato.
    Nunca se envía el documento completo al motor de IA."""
    index: int
    text: str
    before: str = ""
    after: str = ""


def segments_from_sentences(sentences: list[Sentence], targets: list[int]) -> list[Segment]:
    out = []
    for i in sorted({i for i in targets if 0 <= i < len(sentences)}):
        out.append(Segment(
            index=i, text=sentences[i].text,
            before=sentences[i - 1].text[-CONTEXT_CHARS:] if i > 0 else "",
            after=sentences[i + 1].text[:CONTEXT_CHARS] if i + 1 < len(sentences) else "",
        ))
    return out


def rewrite_segments(segments: list[Segment], institution: Institution, client,
                     mode: str = "fluido", budget: float | None = None,
                     raise_rate_limit: bool = True) -> dict:
    """
    Reescribe segmentos en lotes de ``BATCH_SIZE`` respetando un presupuesto de tiempo.

    * ``LLMRateLimitError`` se propaga (si ``raise_rate_limit``) para que la API responda
      429 + Retry-After y el cliente reintente ese lote, en vez de degradar la calidad.
    * Timeout, clave inválida u otros fallos del motor de IA => respaldo local por reglas y
      ``warning`` explicativo, para que el usuario siempre obtenga un resultado.
    """
    mode = mode if mode in MODES else "fluido"
    if not segments:
        return {"engine": "none", "mode": mode, "rewrites": []}
    if not (client and client.enabled):
        return _rewrite_local(segments)

    deadline = time.monotonic() + budget if budget else None
    rewrites: list[dict] = []
    for b in range(0, len(segments), BATCH_SIZE):
        batch = segments[b:b + BATCH_SIZE]
        remaining = (deadline - time.monotonic()) if deadline else None
        try:
            if remaining is not None and remaining < 4:
                raise LLMTimeoutError("Presupuesto de tiempo agotado")
            rewrites += _rewrite_with_gemini(batch, institution, client, mode, remaining)
        except LLMRateLimitError:
            if raise_rate_limit:
                raise
            return _with_local_fallback(rewrites, segments[b:], "cuota de uso excedida temporalmente", client)
        except LLMTimeoutError:
            return _with_local_fallback(rewrites, segments[b:], "el motor de IA tardó demasiado", client)
        except LLMAuthError as exc:
            return _with_local_fallback(rewrites, segments[b:], str(exc), client)
        except LLMError as exc:
            log.warning("Lote de reescritura falló: %s", exc)
            return _with_local_fallback(rewrites, segments[b:], "respuesta inválida del motor de IA", client)
    return {"engine": _engine(client), "mode": mode, "rewrites": rewrites}


def _engine(client) -> str:
    return f"{getattr(client, 'name', 'ia')}:{client.model}"


def _with_local_fallback(done: list[dict], pending: list[Segment], reason: str, client) -> dict:
    local = _rewrite_local(pending)
    return {"engine": f"{_engine(client)}+local-rules" if done else "local-rules",
            "mode": "mixto" if done else "reglas",
            "rewrites": done + local["rewrites"],
            "warning": f"{len(pending)} oración(es) procesadas con el motor local: {reason}."}


def rewrite_sentences(sentences: list[Sentence], targets: list[int], institution: Institution,
                      client, mode: str = "fluido",
                      budget: float | None = None, raise_rate_limit: bool = False) -> dict:
    """Compatibilidad: reescribe por índices a partir de la lista completa de oraciones."""
    return rewrite_segments(segments_from_sentences(sentences, targets), institution, client,
                            mode, budget, raise_rate_limit)


def _rewrite_with_gemini(batch: list[Segment], institution: Institution, client,
                         mode: str, budget: float | None) -> list[dict]:
    items = [{"index": s.index, "before": s.before[-CONTEXT_CHARS:], "text": s.text,
              "after": s.after[:CONTEXT_CHARS]} for s in batch]
    prompt = f"""NORMA INSTITUCIONAL: {institution.name}
Estilo de citación: {institution.citation_style}
Guía de estilo: {institution.style_guide}
Modo de reescritura: {MODES[mode]}

Reescribe SOLO el campo "text" de cada elemento. "before" y "after" son contexto para mantener
la cohesión y NO se reescriben. Varía la longitud respecto de las oraciones vecinas.
Devuelve un arreglo JSON con este formato:
[{{"index": <int>, "rewritten": "<oración reescrita>", "changes": ["<cambio breve>", ...]}}]

ELEMENTOS:
{json.dumps(items, ensure_ascii=False)}"""
    # ~400 tokens de salida por oración es holgado y evita respuestas truncadas.
    data = client.generate_json(_SYSTEM, prompt, temperature=0.75,
                                max_output_tokens=min(8192, 600 + 450 * len(batch)),
                                retries=1, budget=budget)
    if isinstance(data, dict):
        data = data.get("rewrites") or data.get("items") or []
    by_index = {s.index: s for s in batch}
    out = []
    for it in data if isinstance(data, list) else []:
        if not isinstance(it, dict):
            continue
        try:
            idx = int(it.get("index"))
        except (TypeError, ValueError):
            continue
        seg = by_index.get(idx)
        new = str(it.get("rewritten", "")).strip()
        if seg and new and not _drops_citations(seg.text, new):
            out.append({"index": idx, "original": seg.text, "rewritten": new,
                        "changes": [str(c)[:160] for c in (it.get("changes") or [])][:5]})
    return out


_CITATION_RE = re.compile(r"\([^()]*\d{4}[a-z]?\)|\[\d+(?:[-–,]\s*\d+)*\]|\(\d+(?:[-–,]\s*\d+)*\)")


def _drops_citations(original: str, rewritten: str) -> bool:
    """Protección: rechaza una reescritura si elimina una cita o un número del original."""
    for cit in _CITATION_RE.findall(original):
        years_or_nums = re.findall(r"\d+", cit)
        if not all(n in rewritten for n in years_or_nums):
            return True
    return False


# --------------------------------------------------------------------------- #
#  Respaldo local basado en reglas
# --------------------------------------------------------------------------- #
_FILLERS = [
    r"\bes importante (?:destacar|señalar|mencionar|resaltar) que\s+",
    r"\bcabe (?:destacar|resaltar|mencionar|señalar) que\s+",
    r"\bvale la pena mencionar que\s+",
    r"\bit is (?:important|worth) (?:to note|noting) that\s+",
]


def _rewrite_local(segments: list[Segment]) -> dict:
    rng = random.Random(42)
    rewrites = []
    for seg in segments:
        i, original = seg.index, seg.text
        new = original
        changes = []
        for pat in _FILLERS:
            if re.search(pat, new, re.I):
                new = re.sub(pat, "", new, flags=re.I)
                new = new[:1].upper() + new[1:]
                changes.append("Se eliminó una fórmula introductoria vacía")
        table = TRANSITIONS_ES if re.search(r"[áéíóúñ]|\b(el|la|de|que)\b", new, re.I) else TRANSITIONS_EN
        for phrase, options in table.items():
            pattern = re.compile(rf"(?<!\w){re.escape(phrase)}(?!\w)", re.I)
            if pattern.search(new):
                repl = rng.choice(options)
                new = pattern.sub(lambda m, r=repl: _match_case(m.group(0), r), new, count=1)
                changes.append(f"«{phrase}» → «{repl}»")
        if len(new.split()) > 38 and "; " in new:
            head, tail = new.split("; ", 1)
            new = f"{head}. {tail[:1].upper()}{tail[1:]}"
            changes.append("Se dividió una oración extensa")
        if new != original:
            rewrites.append({"index": i, "original": original, "rewritten": new, "changes": changes})
    return {"engine": "local-rules", "mode": "reglas", "rewrites": rewrites,
            "note": "Configure un motor de IA (GEMINI_API_KEY o ANTHROPIC_API_KEY) para obtener paráfrasis completas."}


def _match_case(src: str, repl: str) -> str:
    return repl[:1].upper() + repl[1:] if src[:1].isupper() else repl


def apply_rewrites(text: str, sentences: list[Sentence], rewrites: list[dict]) -> str:
    """Sustituye oraciones por offsets (de atrás hacia adelante) preservando párrafos."""
    by_index = {r["index"]: r["rewritten"] for r in rewrites if r.get("rewritten")}
    out = text
    for s in sorted(sentences, key=lambda s: s.start, reverse=True):
        if s.index in by_index:
            out = out[:s.start] + by_index[s.index] + out[s.end:]
    return out
