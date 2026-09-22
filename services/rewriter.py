"""
Motor de reescritura / parafraseo con estilo institucional.

Modo principal: Gemini 2.5 con instrucciones de estilo de la institución.
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

import logging
import random
import re

from .gemini_client import GeminiClient, GeminiError
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

BATCH_SIZE = 25


def rewrite_sentences(sentences: list[Sentence], targets: list[int], institution: Institution,
                      client: GeminiClient | None, mode: str = "fluido") -> dict:
    targets = sorted({i for i in targets if 0 <= i < len(sentences)})
    if not targets:
        return {"engine": "none", "rewrites": []}
    mode = mode if mode in MODES else "fluido"

    if client and client.enabled:
        try:
            rewrites = []
            for b in range(0, len(targets), BATCH_SIZE):
                rewrites += _rewrite_with_gemini(sentences, targets[b:b + BATCH_SIZE],
                                                 institution, client, mode)
            return {"engine": f"gemini:{client.model}", "mode": mode, "rewrites": rewrites}
        except GeminiError as exc:
            log.warning("Reescritura Gemini falló, se usa respaldo local: %s", exc)
            result = _rewrite_local(sentences, targets)
            result["warning"] = f"Gemini no disponible ({exc}); se aplicó el motor local."
            return result
    return _rewrite_local(sentences, targets)


def _rewrite_with_gemini(sentences, targets, institution, client, mode) -> list[dict]:
    items = []
    for i in targets:
        prev_s = sentences[i - 1].text if i > 0 else ""
        next_s = sentences[i + 1].text if i + 1 < len(sentences) else ""
        items.append(f'{{"index": {i}, "before": {_q(prev_s[-220:])}, '
                     f'"text": {_q(sentences[i].text)}, "after": {_q(next_s[:220])}}}')
    prompt = f"""NORMA INSTITUCIONAL: {institution.name}
Estilo de citación: {institution.citation_style}
Guía de estilo: {institution.style_guide}
Modo de reescritura: {MODES[mode]}

Reescribe SOLO el campo "text" de cada elemento. "before" y "after" son contexto para mantener
la cohesión y NO se reescriben. Varía la longitud respecto de las oraciones vecinas.
Devuelve un arreglo JSON con este formato:
[{{"index": <int>, "rewritten": "<oración reescrita>", "changes": ["<cambio breve>", ...]}}]

ELEMENTOS:
[{", ".join(items)}]"""
    data = client.generate_json(_SYSTEM, prompt, temperature=0.75, max_output_tokens=8192)
    if isinstance(data, dict):
        data = data.get("rewrites") or data.get("items") or []
    valid = set(targets)
    out = []
    for it in data or []:
        try:
            idx = int(it.get("index"))
        except (TypeError, ValueError, AttributeError):
            continue
        new = str(it.get("rewritten", "")).strip()
        if idx in valid and new and not _drops_citations(sentences[idx].text, new):
            out.append({"index": idx, "original": sentences[idx].text, "rewritten": new,
                        "changes": [str(c)[:160] for c in (it.get("changes") or [])][:5]})
    return out


def _q(s: str) -> str:
    import json
    return json.dumps(s, ensure_ascii=False)


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


def _rewrite_local(sentences: list[Sentence], targets: list[int]) -> dict:
    rng = random.Random(42)
    rewrites = []
    for i in targets:
        original = sentences[i].text
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
            "note": "Configure GEMINI_API_KEY para obtener paráfrasis completas con estilo institucional."}


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
