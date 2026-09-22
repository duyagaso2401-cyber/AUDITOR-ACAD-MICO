"""
Cotejo contra literatura científica y repositorios de acceso abierto.

Fuentes consultadas (en paralelo):
    * OpenAlex            – >250 M de obras (artículos, tesis, repositorios). Desde feb-2026
                            exige OPENALEX_API_KEY (gratis: 100 000 créditos/día).
    * Crossref            – metadatos y resúmenes de DOIs registrados. Gratis.
    * Semantic Scholar    – grafo académico con resúmenes. Gratis (clave opcional).
    * Google Scholar      – vía Serper.dev (SERPER_API_KEY), opcional.
    * Web abierta         – búsqueda de frase exacta vía Serper (tesis en repositorios
                            institucionales, sitios web, PDFs), opcional.

Proceso:
    1. Se eligen los fragmentos más "distintivos" del documento (oraciones largas
       con baja proporción de palabras vacías).
    2. Cada fragmento se consulta en las APIs.
    3. Cada resultado (título + resumen + snippet) se compara con el fragmento
       usando *containment* de trigramas (copia literal) y coseno TF (paráfrasis).
    4. Se calcula un índice de similitud muestral ponderado por palabras.

Limitación honesta: las APIs abiertas indexan títulos y resúmenes, no el texto
completo de cada obra; por eso el índice es una *estimación muestral* y no
sustituye a un repositorio privado de texto completo como el de Turnitin.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from .text_utils import (STOPWORDS_EN, STOPWORDS_ES, Sentence, clamp, containment,
                         cosine_tf, tokenize)

log = logging.getLogger(__name__)

HTTP_TIMEOUT = float(os.getenv("SOURCES_TIMEOUT", "8"))
CONTACT = os.getenv("CONTACT_EMAIL", "auditor@example.org")
USER_AGENT = f"AuditorAcademico/1.0 (mailto:{CONTACT})"
MATCH_THRESHOLD = 0.30        # similitud mínima para considerar coincidencia
RELATED_THRESHOLD = 0.12      # por debajo: fuente relacionada (sugerencia de cita)

_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

# Caché en memoria con TTL (evita repetir consultas idénticas y respeta rate-limits).
_CACHE: dict[str, tuple[float, list]] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL = 60 * 60 * 6


def _cached(key: str, fn):
    h = hashlib.sha1(key.encode()).hexdigest()
    with _CACHE_LOCK:
        hit = _CACHE.get(h)
        if hit and time.time() - hit[0] < _CACHE_TTL:
            return hit[1]
    value = fn()
    with _CACHE_LOCK:
        if len(_CACHE) > 2000:
            _CACHE.clear()
        _CACHE[h] = (time.time(), value)
    return value


def _clean_html(s: str | None) -> str:
    return re.sub(r"<[^>]+>", " ", s or "").strip()


# --------------------------------------------------------------------------- #
#  Conectores de fuentes
# --------------------------------------------------------------------------- #
def search_openalex(query: str, limit: int = 5) -> list[dict]:
    params = {"search": query, "per_page": limit,
              "select": "id,doi,title,publication_year,type,abstract_inverted_index,"
                        "authorships,primary_location"}
    if os.getenv("OPENALEX_API_KEY"):
        params["api_key"] = os.getenv("OPENALEX_API_KEY")
    r = _session.get("https://api.openalex.org/works", params=params, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    out = []
    for w in r.json().get("results", []):
        inv = w.get("abstract_inverted_index") or {}
        abstract = ""
        if inv:
            pos = {p: word for word, ps in inv.items() for p in ps}
            abstract = " ".join(pos[i] for i in sorted(pos))
        loc = (w.get("primary_location") or {})
        venue = ((loc.get("source") or {}) or {}).get("display_name")
        out.append({
            "source": "OpenAlex", "title": w.get("title") or "", "abstract": abstract,
            "url": w.get("doi") or loc.get("landing_page_url") or w.get("id"),
            "year": w.get("publication_year"), "type": w.get("type"), "venue": venue,
            "authors": [a.get("author", {}).get("display_name") for a in (w.get("authorships") or [])[:4]],
        })
    return out


def search_crossref(query: str, limit: int = 5) -> list[dict]:
    params = {"query.bibliographic": query, "rows": limit, "mailto": CONTACT,
              "select": "DOI,title,abstract,author,issued,URL,container-title,type"}
    r = _session.get("https://api.crossref.org/works", params=params, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    out = []
    for it in r.json().get("message", {}).get("items", []):
        year = None
        parts = (it.get("issued") or {}).get("date-parts") or [[None]]
        if parts and parts[0]:
            year = parts[0][0]
        out.append({
            "source": "Crossref", "title": " ".join(it.get("title") or []),
            "abstract": _clean_html(it.get("abstract")), "url": it.get("URL"),
            "year": year, "type": it.get("type"),
            "venue": " ".join(it.get("container-title") or []),
            "authors": [f"{a.get('given', '')} {a.get('family', '')}".strip() for a in (it.get("author") or [])[:4]],
        })
    return out


def search_semantic_scholar(query: str, limit: int = 5) -> list[dict]:
    headers = {}
    if os.getenv("SEMANTIC_SCHOLAR_API_KEY"):
        headers["x-api-key"] = os.getenv("SEMANTIC_SCHOLAR_API_KEY")
    params = {"query": query[:300], "limit": limit,
              "fields": "title,abstract,url,year,venue,authors,externalIds,publicationTypes"}
    r = _session.get("https://api.semanticscholar.org/graph/v1/paper/search",
                     params=params, headers=headers, timeout=HTTP_TIMEOUT)
    if r.status_code == 429:
        return []  # plan sin clave: límite compartido; se ignora silenciosamente
    r.raise_for_status()
    out = []
    for p in r.json().get("data", []) or []:
        doi = (p.get("externalIds") or {}).get("DOI")
        out.append({
            "source": "Semantic Scholar", "title": p.get("title") or "",
            "abstract": p.get("abstract") or "",
            "url": f"https://doi.org/{doi}" if doi else p.get("url"),
            "year": p.get("year"), "type": ", ".join(p.get("publicationTypes") or []) or None,
            "venue": p.get("venue"), "authors": [a.get("name") for a in (p.get("authors") or [])[:4]],
        })
    return out


def _serper(endpoint: str, payload: dict) -> dict:
    key = os.getenv("SERPER_API_KEY")
    if not key:
        return {}
    r = _session.post(f"https://google.serper.dev/{endpoint}", json=payload,
                      headers={"X-API-KEY": key, "Content-Type": "application/json"},
                      timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()


def search_google_scholar(query: str, limit: int = 5) -> list[dict]:
    data = _serper("scholar", {"q": query, "num": limit})
    return [{
        "source": "Google Scholar", "title": it.get("title", ""), "abstract": it.get("snippet", ""),
        "url": it.get("link"), "year": it.get("year"), "type": "scholar",
        "venue": it.get("publicationInfo"), "authors": [],
    } for it in (data.get("organic") or [])[:limit]]


def search_web_exact(query: str, limit: int = 5) -> list[dict]:
    """Frase exacta entre comillas: detecta copia literal en repositorios y web abierta."""
    phrase = " ".join(query.split()[:28])
    data = _serper("search", {"q": f"\"{phrase}\"", "num": limit})
    return [{
        "source": "Web / Repositorios", "title": it.get("title", ""),
        "abstract": it.get("snippet", ""), "url": it.get("link"), "year": None,
        "type": "web", "venue": re.sub(r"^https?://(www\.)?([^/]+).*", r"\2", it.get("link", "")),
        "authors": [],
    } for it in (data.get("organic") or [])[:limit]]


PROVIDERS = {
    "openalex": search_openalex,
    "crossref": search_crossref,
    "semantic_scholar": search_semantic_scholar,
    "google_scholar": search_google_scholar,
    "web": search_web_exact,
}


def available_providers() -> list[str]:
    provs = ["openalex", "crossref", "semantic_scholar"]
    if os.getenv("SERPER_API_KEY"):
        provs += ["google_scholar", "web"]
    return provs


# --------------------------------------------------------------------------- #
#  Selección de fragmentos y cotejo
# --------------------------------------------------------------------------- #
def select_fragments(sentences: list[Sentence], lang: str, max_fragments: int = 8) -> list[Sentence]:
    stop = STOPWORDS_ES if lang == "es" else STOPWORDS_EN
    scored = []
    for s in sentences:
        words = s.words
        if not 10 <= len(words) <= 60:
            continue
        content = [w for w in words if w not in stop and len(w) > 3]
        distinctiveness = len(set(content)) / len(words)
        has_citation = bool(re.search(r"\(\s*[A-ZÁÉÍÓÚ][^)]*\d{4}\s*\)|\[\d+\]", s.text))
        scored.append((distinctiveness + (0.15 if has_citation else 0), s))
    scored.sort(key=lambda x: x[0], reverse=True)
    chosen = sorted((s for _, s in scored[:max_fragments]), key=lambda s: s.index)
    return chosen


def _query_for(sentence: Sentence, lang: str) -> str:
    stop = STOPWORDS_ES if lang == "es" else STOPWORDS_EN
    words = tokenize(sentence.text, lower=False)
    key = [w for w in words if w.lower() not in stop]
    return " ".join(key[:18])


def _similarity(fragment_words: list[str], candidate: dict, lang: str) -> float:
    stop = STOPWORDS_ES if lang == "es" else STOPWORDS_EN
    cand_words = tokenize(f"{candidate.get('title', '')} {candidate.get('abstract', '')}")
    if not cand_words:
        return 0.0
    lit = containment(fragment_words, cand_words, 3)
    sem = cosine_tf(fragment_words, cand_words, stop)
    # Un coseno alto con resúmenes largos puede ser coincidencia temática:
    # se atenúa, y la copia literal (trigramas) domina.
    return clamp(max(lit, 0.75 * lit + 0.55 * sem, 0.6 * sem))


def check_plagiarism(sentences: list[Sentence], lang: str, providers: list[str] | None = None,
                     max_fragments: int = 8, similarity_threshold: int = 20) -> dict:
    providers = [p for p in (providers or available_providers()) if p in PROVIDERS]
    fragments = select_fragments(sentences, lang, max_fragments)
    if not fragments:
        return {"available": False, "error": "No hay fragmentos suficientemente extensos para cotejar",
                "similarity_index": 0, "fragments": [], "sources": [], "providers": providers}

    jobs = {}
    errors: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=min(12, len(fragments) * len(providers))) as pool:
        for frag in fragments:
            q = _query_for(frag, lang) if providers else ""
            for prov in providers:
                query = frag.text if prov == "web" else q
                fut = pool.submit(_cached, f"{prov}|{query}", lambda p=prov, qq=query: PROVIDERS[p](qq))
                jobs[fut] = (frag, prov)
        results: dict[int, list[dict]] = {f.index: [] for f in fragments}
        for fut in as_completed(jobs):
            frag, prov = jobs[fut]
            try:
                results[frag.index].extend(fut.result() or [])
            except Exception as exc:  # noqa: BLE001 – una fuente caída no debe tumbar la auditoría
                errors[prov] = str(exc)[:160]
                log.info("Proveedor %s falló: %s", prov, exc)

    fragment_reports = []
    source_index: dict[str, dict] = {}
    matched_words = 0.0
    analyzed_words = 0
    for frag in fragments:
        fw = frag.words
        analyzed_words += len(fw)
        best = None
        candidates = []
        for cand in results.get(frag.index, []):
            sim = _similarity(fw, cand, lang)
            if sim < RELATED_THRESHOLD:
                continue
            item = {**cand, "similarity": round(sim * 100, 1),
                    "abstract": (cand.get("abstract") or "")[:400]}
            candidates.append(item)
            if not best or sim > best["similarity"] / 100:
                best = item
            key = (cand.get("url") or cand.get("title") or "").lower()
            if key:
                agg = source_index.setdefault(key, {**item, "fragments": [], "max_similarity": 0})
                agg["fragments"].append(frag.index)
                agg["max_similarity"] = max(agg["max_similarity"], item["similarity"])
        candidates.sort(key=lambda c: c["similarity"], reverse=True)
        best_sim = (best["similarity"] / 100) if best else 0.0
        if best_sim >= MATCH_THRESHOLD:
            matched_words += len(fw) * best_sim
        fragment_reports.append({
            "index": frag.index, "text": frag.text, "best_similarity": round(best_sim * 100, 1),
            "status": "coincidencia" if best_sim >= MATCH_THRESHOLD else
                      "relacionada" if best_sim >= RELATED_THRESHOLD else "original",
            "matches": candidates[:4],
        })

    similarity_index = round(100 * matched_words / analyzed_words, 1) if analyzed_words else 0.0
    sources = sorted(source_index.values(), key=lambda s: s["max_similarity"], reverse=True)[:25]
    return {
        "available": True,
        "providers": providers,
        "provider_errors": errors,
        "similarity_index": similarity_index,
        "within_threshold": similarity_index <= similarity_threshold,
        "fragments_analyzed": len(fragments),
        "fragments": fragment_reports,
        "sources": sources,
        "method": ("Índice muestral: fragmentos distintivos cotejados por trigramas (copia literal) "
                   "y coseno TF (paráfrasis) contra títulos/resúmenes de literatura abierta."),
    }
