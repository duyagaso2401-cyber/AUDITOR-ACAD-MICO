"""
Auditor Académico – Plataforma web de auditoría de integridad académica.

    * Detección dual de IA (métricas locales de burstiness/perplejidad + Gemini 2.5 o Claude).
    * Cotejo de similitud contra OpenAlex, Crossref, Semantic Scholar y Google Scholar/Web (Serper).
    * Reescritura con estilo institucional (UPEL, APA 7, IEEE, Vancouver) y exportación .docx.
    * Dashboard web y API REST versionada (/api/v1).

Ejecución local:   python app.py
Producción:        gunicorn app:app  (ver Procfile / render.yaml)
"""
from __future__ import annotations

import hashlib
import hmac
import re
import logging
import os
import secrets
import threading
import time
import uuid
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from functools import wraps

from dotenv import load_dotenv
from flask import (Flask, Response, g, jsonify, render_template, request, send_file,
                   session)
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge
from werkzeug.middleware.proxy_fix import ProxyFix

from services.ai_detector import detect_ai
from services.documents import ExtractionError, build_docx, extract_text
from services.claude_client import ClaudeClient
from services.gemini_client import GeminiClient
from services.llm import QUOTA_MESSAGE, LLMRateLimitError, LLMRegistry, notice_of
from services.institutions import get_institution, list_institutions
from services.plagiarism import available_providers, check_plagiarism
from services.projects import ProjectError, create_store, normalize_project
from services.rewriter import (BATCH_SIZE, MAX_SEGMENT_CHARS, MODES, Segment, apply_rewrites,
                               rewrite_segments, rewrite_sentences, segments_from_sentences)
from services.text_utils import detect_language, normalize, split_sentences

load_dotenv()

APP_VERSION = "1.0.0"
MAX_TEXT_CHARS = int(os.getenv("MAX_TEXT_CHARS", "80000"))
MIN_TEXT_CHARS = 80
RATE_LIMIT_PER_MIN = int(os.getenv("RATE_LIMIT_PER_MIN", "20"))
# Reescritura: máximo de oraciones por petición HTTP y segundos máximos por petición.
# 5 oraciones ≈ 1-2 lotes del motor de IA ≈ 4-10 s: muy por debajo del timeout del proxy.
REWRITE_MAX_PER_REQUEST = int(os.getenv("REWRITE_MAX_PER_REQUEST", "5"))
REWRITE_TIME_BUDGET = float(os.getenv("REWRITE_TIME_BUDGET", "20"))
# Las peticiones de reescritura por lotes son muchas y pequeñas: tienen su propio límite.
REWRITE_RATE_LIMIT_PER_MIN = int(os.getenv("REWRITE_RATE_LIMIT_PER_MIN", "40"))
API_KEYS = {k.strip() for k in os.getenv("API_KEYS", "").split(",") if k.strip()}
CORS_ORIGINS = {o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()}

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
log = logging.getLogger("auditor")

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)  # Render usa proxy inverso
app.config.update(
    SECRET_KEY=os.getenv("SECRET_KEY") or secrets.token_hex(32),
    MAX_CONTENT_LENGTH=int(os.getenv("MAX_UPLOAD_MB", "15")) * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("FLASK_ENV") != "development",
    JSON_AS_ASCII=False,
)
app.json.ensure_ascii = False
app.json.sort_keys = False

gemini = GeminiClient()
claude = ClaudeClient()
llms = LLMRegistry({"gemini": gemini, "claude": claude})   # selector multi-LLM (parámetro "provider")
_executor = ThreadPoolExecutor(max_workers=int(os.getenv("WORKERS_THREADS", "8")))


# --------------------------------------------------------------------------- #
#  Infraestructura: IDs de petición, rate-limit, autenticación, CORS, errores
# --------------------------------------------------------------------------- #
class RateLimiter:
    """Ventana deslizante en memoria por cliente (suficiente para 1 instancia en Render).
    Para escalar horizontalmente, reemplazar por Redis (Render Key Value)."""

    def __init__(self, limit: int, window: int = 60):
        self.limit, self.window = limit, window
        self.hits: dict[str, deque] = defaultdict(deque)
        self.lock = threading.Lock()

    def allow(self, key: str) -> tuple[bool, int]:
        now = time.time()
        with self.lock:
            q = self.hits[key]
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.limit:
                return False, int(self.window - (now - q[0])) + 1
            q.append(now)
            if len(self.hits) > 10000:
                self.hits.clear()
            return True, 0


limiter = RateLimiter(RATE_LIMIT_PER_MIN)
rewrite_limiter = RateLimiter(REWRITE_RATE_LIMIT_PER_MIN)
projects_limiter = RateLimiter(int(os.getenv("PROJECTS_RATE_LIMIT_PER_MIN", "60")))
project_store = create_store()


def api_error(message: str, status: int = 400, **extra):
    payload = {"ok": False, "error": message, "request_id": getattr(g, "request_id", None), **extra}
    return jsonify(payload), status


def _valid_api_key(key: str | None) -> bool:
    return bool(key) and any(hmac.compare_digest(key, k) for k in API_KEYS)


def protected(rate_limited: bool = True, bucket: RateLimiter | None = None):
    """Autoriza por X-API-Key (integraciones) o por sesión del dashboard.
    Si API_KEYS no está definida, la API queda abierta (modo desarrollo/demo)."""
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            key = request.headers.get("X-API-Key") or request.args.get("api_key")
            if API_KEYS and not (session.get("ui") or _valid_api_key(key)):
                return api_error("API key inválida o ausente (cabecera X-API-Key)", 401)
            if rate_limited:
                client = key or request.remote_addr or "anon"
                ok, retry = (bucket or limiter).allow(client)
                if not ok:
                    resp, status = api_error("Límite de peticiones excedido", 429, retry_after=retry)
                    resp.headers["Retry-After"] = str(retry)
                    return resp, status
            return fn(*args, **kwargs)
        return wrapper
    return decorator


@app.before_request
def _before():
    g.request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
    g.t0 = time.perf_counter()


@app.after_request
def _after(resp: Response):
    resp.headers["X-Request-ID"] = g.get("request_id", "")
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    resp.headers["X-Frame-Options"] = "SAMEORIGIN"
    origin = request.headers.get("Origin")
    if request.path.startswith("/api/") and origin and ("*" in CORS_ORIGINS or origin in CORS_ORIGINS):
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Vary"] = "Origin"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-API-Key, X-Request-ID, X-Client-Id"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
        resp.headers["Access-Control-Expose-Headers"] = "X-Request-ID, Content-Disposition"
    if request.path.startswith("/api/"):
        log.info("%s %s %s %.0fms", request.method, request.path, resp.status_code,
                 (time.perf_counter() - g.get("t0", time.perf_counter())) * 1000)
    return resp


@app.route("/api/<path:_any>", methods=["OPTIONS"])
def _preflight(_any):
    return Response(status=204)


@app.errorhandler(RequestEntityTooLarge)
def _too_large(_e):
    return api_error(f"Archivo demasiado grande (máx. {app.config['MAX_CONTENT_LENGTH'] // 1048576} MB)", 413)


@app.errorhandler(HTTPException)
def _http_error(e: HTTPException):
    if request.path.startswith("/api/"):
        return api_error(e.description or e.name, e.code or 500)
    return e


@app.errorhandler(Exception)
def _unhandled(e: Exception):
    log.exception("Error no controlado: %s", e)
    if request.path.startswith("/api/"):
        return api_error("Error interno del servidor", 500)
    return "Error interno", 500


# --------------------------------------------------------------------------- #
#  Lógica de negocio
# --------------------------------------------------------------------------- #
def _read_input() -> tuple[str, dict]:
    """Acepta JSON {text, ...} o multipart (campo 'file' y/o 'text' + campos de opciones)."""
    if request.files.get("file"):
        f = request.files["file"]
        text = extract_text(f.filename or "doc.txt", f.read())
        opts = request.form.to_dict()
    else:
        body = request.get_json(silent=True) or {}
        text = body.get("text", "")
        opts = body
    return normalize(text or ""), opts


def _as_bool(value, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "si", "sí", "on"}


def _llm_info(requested: str, client, notice: str | None) -> dict:
    return {"requested": requested,
            "used": getattr(client, "name", None) if client is not None else None,
            "model": getattr(client, "model", None) if client is not None else None,
            "notice": notice_of(client, notice)}


def run_audit(text: str, opts: dict) -> dict:
    institution = get_institution(opts.get("institution"))
    # "provider" = motor de IA (gemini | claude). No confundir con "providers" (fuentes de cotejo).
    llm, llm_notice, llm_requested = llms.for_request(opts.get("provider") or opts.get("ai_provider"))
    use_semantic = _as_bool(opts.get("use_semantic", opts.get("use_gemini")), True)
    do_plagiarism = _as_bool(opts.get("check_plagiarism"), True)
    auto_rewrite = _as_bool(opts.get("rewrite"), False)
    max_fragments = max(1, min(int(opts.get("max_fragments", 8) or 8), 20))
    providers = opts.get("providers")
    if isinstance(providers, str):
        providers = [p.strip() for p in providers.split(",") if p.strip()]

    t0 = time.perf_counter()
    lang = detect_language(text)
    sentences = split_sentences(text)

    ai_future = _executor.submit(detect_ai, sentences, text, lang, institution, llm, use_semantic)
    pl_future = (_executor.submit(check_plagiarism, sentences, lang, providers, max_fragments,
                                  institution.similarity_threshold)
                 if do_plagiarism else None)
    ai = ai_future.result()
    plagiarism = pl_future.result() if pl_future else {"available": False, "error": "Desactivado",
                                                       "similarity_index": 0, "fragments": [], "sources": []}

    # Mapa de texto: estado combinado por oración (para el subrayado del dashboard).
    frag_map = {f["index"]: f for f in plagiarism.get("fragments", [])}
    ai_map = {s["index"]: s for s in ai["per_sentence"]}
    sentence_map = []
    for s in sentences:
        a = ai_map.get(s.index, {"score": 0, "reasons": []})
        fr = frag_map.get(s.index)
        sentence_map.append({
            **s.to_dict(),
            "ai_score": a["score"],
            "ai_reasons": a["reasons"],
            "plagiarism": ({"status": fr["status"], "similarity": fr["best_similarity"],
                            "top_match": (fr["matches"][0] if fr["matches"] else None)} if fr else None),
            "flag": ("plagio" if fr and fr["status"] == "coincidencia" else
                     "ia_alta" if a["score"] >= 60 else
                     "ia_media" if a["score"] >= 45 else
                     "relacionada" if fr and fr["status"] == "relacionada" else "ok"),
        })

    targets = [s["index"] for s in sentence_map if s["flag"] in ("plagio", "ia_alta", "ia_media")]
    rewrite = None
    if auto_rewrite and targets:
        rewrite = rewrite_sentences(sentences, targets[:REWRITE_MAX_PER_REQUEST * 3], institution, llm,
                                    opts.get("mode", "fluido"), budget=REWRITE_TIME_BUDGET)
        rewrite["corrected_text"] = apply_rewrites(text, sentences, rewrite["rewrites"])

    ai_pct = ai["ai_probability"]
    sim_pct = plagiarism.get("similarity_index", 0) or 0
    integrity = round(max(0.0, 100 - (0.6 * ai_pct + 0.4 * min(100, sim_pct * 2))), 1)
    return {
        "ok": True,
        "request_id": g.get("request_id"),
        "version": APP_VERSION,
        "institution": institution.to_public(),
        "language": lang,
        "llm": _llm_info(llm_requested, llm, llm_notice),
        "summary": {
            "ai_probability": ai_pct,
            "ai_level": ai["level"],
            "similarity_index": sim_pct,
            "integrity_score": integrity,
            "words": ai["local"]["metrics"]["words"],
            "sentences": len(sentences),
            "flagged_sentences": len(targets),
            "verdict": _verdict(ai_pct, sim_pct, institution),
            "elapsed_ms": round((time.perf_counter() - t0) * 1000),
        },
        "ai": ai,
        "plagiarism": plagiarism,
        "sentences": sentence_map,
        "rewrite_targets": targets,
        "rewrite": rewrite,
        "text": text,
    }


def _verdict(ai_pct: float, sim_pct: float, inst) -> str:
    if ai_pct <= inst.ai_threshold and sim_pct <= inst.similarity_threshold:
        return "APROBADO – dentro de los umbrales institucionales"
    if ai_pct >= 60 or sim_pct >= inst.similarity_threshold * 2:
        return "REVISIÓN OBLIGATORIA – indicios altos de IA o similitud"
    return "OBSERVACIONES – requiere ajustes de redacción o citación"


def _validate_text(text: str):
    if len(text) < MIN_TEXT_CHARS:
        return api_error(f"El texto debe tener al menos {MIN_TEXT_CHARS} caracteres", 422)
    if len(text) > MAX_TEXT_CHARS:
        return api_error(f"El texto excede el máximo de {MAX_TEXT_CHARS} caracteres", 413)
    return None


# --------------------------------------------------------------------------- #
#  Rutas web
# --------------------------------------------------------------------------- #
@app.get("/")
def index():
    session["ui"] = True  # autoriza al dashboard a usar la API sin exponer claves
    return render_template("index.html", institutions=list_institutions(),
                           gemini_enabled=gemini.enabled, llm_status=llms.status(),
                           llm_any=llms.any_enabled(), providers=available_providers(),
                           llm_active=[st["label"].split(" ")[-1] for st in llms.status().values() if st["enabled"]],
                           modes=MODES, version=APP_VERSION)


@app.get("/health")
def health():
    return jsonify({"status": "ok", "version": APP_VERSION, "llm": llms.status(),
                    "providers": available_providers()})


# --------------------------------------------------------------------------- #
#  API REST v1
# --------------------------------------------------------------------------- #
@app.get("/api/v1")
def api_index():
    return jsonify({
        "name": "Auditor Académico API", "version": APP_VERSION,
        "endpoints": {
            "POST /api/v1/audit": "Auditoría completa (IA + similitud [+ reescritura]). JSON o multipart.",
            "POST /api/v1/rewrite": "Reescribe oraciones indicadas con estilo institucional.",
            "POST /api/v1/export": "Genera .docx corregido (con anexo de informe opcional).",
            "POST /api/v1/extract": "Extrae texto de .docx/.pdf/.txt.",
            "GET  /api/v1/institutions": "Perfiles institucionales disponibles.",
            "POST /api/v1/projects/save": "Guarda/actualiza un proyecto (borrador) con su estado completo.",
            "GET  /api/v1/projects/load?id=": "Recupera un proyecto guardado.",
            "GET  /api/v1/projects": "Lista proyectos (filtros: institucion_id, docente_id, asignatura, grado, status).",
            "DELETE /api/v1/projects/<id>": "Elimina un proyecto.",
        },
        "auth": "Cabecera X-API-Key" if API_KEYS else "Abierta (defina API_KEYS en producción)",
    })


@app.get("/api/v1/institutions")
def api_institutions():
    return jsonify({"ok": True, "institutions": list_institutions()})


@app.post("/api/v1/extract")
@protected()
def api_extract():
    f = request.files.get("file")
    if not f:
        return api_error("Adjunte un archivo en el campo 'file'")
    try:
        text = extract_text(f.filename or "", f.read())
    except ExtractionError as exc:
        return api_error(str(exc), 422)
    return jsonify({"ok": True, "filename": f.filename, "chars": len(text), "text": text[:MAX_TEXT_CHARS],
                    "truncated": len(text) > MAX_TEXT_CHARS})


@app.post("/api/v1/audit")
@protected()
def api_audit():
    """
    Cuerpo JSON:
        {
          "text": "...",                         # o multipart con 'file'
          "institution": "upel|unicartagena_apa7|ieee|vancouver",
          "provider": "gemini|claude",          # motor de IA (por defecto gemini)
          "use_semantic": true,
          "check_plagiarism": true,
          "providers": ["openalex","crossref","semantic_scholar","google_scholar","web"],
          "max_fragments": 8,
          "rewrite": false,
          "mode": "fluido|academico|conciso"
        }
    """
    try:
        text, opts = _read_input()
    except ExtractionError as exc:
        return api_error(str(exc), 422)
    if (err := _validate_text(text)):
        return err
    report = run_audit(text, opts)
    if _as_bool(opts.get("compact"), False):
        report.pop("text", None)
        report["sentences"] = [{k: s[k] for k in ("index", "ai_score", "flag")} for s in report["sentences"]]
    return jsonify(report)


def _parse_segments(raw) -> list[Segment]:
    """Valida el formato ligero: [{"index": int, "text": str, "before"?: str, "after"?: str}]."""
    if not isinstance(raw, list) or not raw:
        raise ValueError("'segments' debe ser una lista no vacía")
    out = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"segments[{i}] debe ser un objeto")
        try:
            index = int(item.get("index"))
        except (TypeError, ValueError):
            raise ValueError(f"segments[{i}].index debe ser un entero") from None
        text = normalize(str(item.get("text") or ""))
        if not text:
            raise ValueError(f"segments[{i}].text está vacío")
        out.append(Segment(index=index, text=text[:MAX_SEGMENT_CHARS],
                           before=str(item.get("before") or "")[-400:],
                           after=str(item.get("after") or "")[:400]))
    return out


@app.post("/api/v1/rewrite")
@protected(bucket=rewrite_limiter)
def api_rewrite():
    """
    Reescritura por lotes pequeños. Cada petición procesa como máximo
    REWRITE_MAX_PER_REQUEST oraciones y responde en ≤ REWRITE_TIME_BUDGET s.

    Modo A – ligero (recomendado, lo usa el dashboard): sólo se envían las oraciones
    y su contexto local, nunca el documento completo.
        {"segments": [{"index": 12, "text": "...", "before": "...", "after": "..."}],
         "institution": "upel", "mode": "fluido"}

    Modo B – por índices (integraciones): se envía el texto y los índices; el servidor
    procesa los primeros N y devuelve "pending" con los que faltan para la siguiente llamada.
        {"text": "...", "indices": [3, 7, 12, ...], "institution": "upel"}

    Parámetro opcional "provider": "gemini" (defecto) | "claude".

    Errores: 400 (petición inválida), 413 (demasiados segmentos), 429 (cuota de uso o
    límite de la API, con Retry-After), 500 (error inesperado, siempre en JSON).
    """
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return api_error("El cuerpo debe ser JSON", 400)
    institution = get_institution(body.get("institution"))
    mode = body.get("mode", "fluido")
    pending: list[int] = []
    corrected_text = None

    try:
        if "segments" in body:
            segments = _parse_segments(body["segments"])
            if len(segments) > REWRITE_MAX_PER_REQUEST:
                return api_error(f"Máximo {REWRITE_MAX_PER_REQUEST} segmentos por petición; "
                                 f"divida la solicitud en lotes", 413,
                                 max_per_request=REWRITE_MAX_PER_REQUEST)
            full_text, sentences = None, None
        else:
            full_text = normalize(body.get("text", ""))
            if (err := _validate_text(full_text)):
                return err
            sentences = split_sentences(full_text)
            indices = body.get("indices")
            if not indices:
                ai = detect_ai(sentences, full_text, detect_language(full_text), institution, None,
                               use_semantic=False)
                indices = [s["index"] for s in ai["per_sentence"] if s["score"] >= 45]
            if not isinstance(indices, list):
                raise ValueError("'indices' debe ser una lista de enteros")
            indices = sorted({int(i) for i in indices if 0 <= int(i) < len(sentences)})
            pending = indices[REWRITE_MAX_PER_REQUEST:]
            segments = segments_from_sentences(sentences, indices[:REWRITE_MAX_PER_REQUEST])
    except (TypeError, ValueError) as exc:
        return api_error(str(exc), 400)

    try:
        llm, llm_notice, llm_requested = llms.for_request(body.get("provider") or body.get("ai_provider"))
        result = rewrite_segments(segments, institution, llm, mode,
                                  budget=REWRITE_TIME_BUDGET, raise_rate_limit=True)
    except LLMRateLimitError as exc:
        retry = max(1, int(round(exc.retry_after)))
        resp, status = api_error(f"{QUOTA_MESSAGE}; reintente este lote en unos segundos", 429,
                                 retry_after=retry, retryable=True)
        resp.headers["Retry-After"] = str(retry)
        return resp, status
    except Exception as exc:  # noqa: BLE001 – nunca un 500 en HTML ni sin contexto
        log.exception("Fallo inesperado en reescritura: %s", exc)
        return api_error("Error inesperado al reescribir este lote", 500, retryable=True)

    if full_text is not None and sentences is not None:
        corrected_text = apply_rewrites(full_text, sentences, result["rewrites"])
    return jsonify({
        "ok": True,
        "institution": institution.id,
        "processed": [s.index for s in segments],
        "pending": pending,
        "done": not pending,
        "max_per_request": REWRITE_MAX_PER_REQUEST,
        "batch_size": BATCH_SIZE,
        "llm": _llm_info(llm_requested, llm, llm_notice),
        **result,
        **({"corrected_text": corrected_text} if corrected_text is not None else {}),
    })


# --------------------------------------------------------------------------- #
#  Proyectos (borradores persistentes)
# --------------------------------------------------------------------------- #
_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9-]{16,64}$")


def _project_owner() -> str:
    """Propietario del proyecto.
    * Integraciones: hash de la API key (nunca se guarda la clave en claro).
    * Dashboard: identificador anónimo del navegador (cabecera X-Client-Id).
    Cuando exista autenticación de usuarios, devolver aquí el user_id y aplicar roles."""
    key = request.headers.get("X-API-Key")
    if key and _valid_api_key(key):
        return "key:" + hashlib.sha256(key.encode()).hexdigest()[:32]
    client_id = request.headers.get("X-Client-Id", "")
    if not _CLIENT_ID_RE.match(client_id):
        raise ProjectError("Falta la cabecera X-Client-Id (16-64 caracteres alfanuméricos)")
    return "client:" + client_id


def _project_error(exc: ProjectError):
    return api_error(str(exc), getattr(exc, "status", 400))


@app.post("/api/v1/projects/save")
@protected(bucket=projects_limiter)
def api_project_save():
    """Crea o actualiza (upsert) un proyecto con su estado completo.
    Respuestas: 201 creado · 200 actualizado · 400 inválido · 404 ajeno · 409 versión obsoleta."""
    body = request.get_json(silent=True)
    try:
        owner = _project_owner()
        project = normalize_project(body)
        meta = project_store.save(project, owner)
    except ProjectError as exc:
        return _project_error(exc)
    return jsonify({"ok": True, "project": meta}), (201 if meta["created"] else 200)


@app.get("/api/v1/projects/load")
@protected(rate_limited=False)
def api_project_load():
    project_id = request.args.get("id", "")
    try:
        owner = _project_owner()
        project = project_store.load(project_id, owner)
    except ProjectError as exc:
        return _project_error(exc)
    return jsonify({"ok": True, "project": project})


@app.get("/api/v1/projects")
@protected(rate_limited=False)
def api_project_list():
    """Lista los proyectos del propietario. Filtros opcionales por contexto académico:
    ?institucion_id=&docente_id=&asignatura=&grado=&status=&limit="""
    try:
        owner = _project_owner()
        limit = int(request.args.get("limit", 20))
    except ProjectError as exc:
        return _project_error(exc)
    except ValueError:
        return api_error("'limit' debe ser un entero", 400)
    return jsonify({"ok": True, "projects": project_store.list(owner, request.args.to_dict(), limit)})


@app.delete("/api/v1/projects/<project_id>")
@protected(rate_limited=False)
def api_project_delete(project_id: str):
    try:
        project_store.delete(project_id, _project_owner())
    except ProjectError as exc:
        return _project_error(exc)
    return jsonify({"ok": True})


@app.post("/api/v1/export")
@protected(rate_limited=False)
def api_export():
    body = request.get_json(silent=True) or {}
    text = normalize(body.get("text", ""))
    if not text:
        return api_error("Falta 'text'")
    institution = get_institution(body.get("institution"))
    data = build_docx(
        text, institution,
        title=str(body.get("title", ""))[:200],
        author=str(body.get("author", ""))[:200],
        report=body.get("report") if _as_bool(body.get("include_report"), True) else None,
        rewrites=body.get("rewrites") or [],
    )
    import io
    fname = (body.get("filename") or "documento_corregido").replace("/", "_")[:80]
    return send_file(io.BytesIO(data), as_attachment=True, download_name=f"{fname}.docx",
                     mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document")


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=os.getenv("FLASK_ENV") == "development")
