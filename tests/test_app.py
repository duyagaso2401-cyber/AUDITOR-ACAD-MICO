"""Pruebas de humo: ejecutar con  `python -m pytest -q`  (sin red: se simulan las APIs externas)."""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("GEMINI_API_KEY", "")
os.environ.setdefault("PROJECTS_DB_PATH", ":memory:")

import app as app_module  # noqa: E402
from services import plagiarism  # noqa: E402
from services.text_utils import split_sentences  # noqa: E402

AI_TEXT = (
    "En la actualidad, la inteligencia artificial juega un papel crucial en la transformación de la "
    "educación superior. Además, es importante destacar que las herramientas digitales permiten fomentar "
    "el aprendizaje autónomo de los estudiantes. Por otro lado, los docentes deben adaptarse a un entorno "
    "cada vez más dinámico y exigente. Asimismo, la integración de tecnologías emergentes potencia la "
    "calidad de los procesos formativos (García, 2021). En conclusión, la inteligencia artificial "
    "representa una oportunidad transformadora para la educación del futuro."
)
HUMAN_TEXT = (
    "Llegué a la escuela de Barinas en marzo de 2019, sin saber mucho de computadoras. Los muchachos, "
    "en cambio, sí sabían. Uno de ellos —Luis, de catorce años— me enseñó a usar el proyector que llevaba "
    "dos años guardado en una caja. ¿Por qué nadie lo había sacado? Nadie tenía la llave del depósito. "
    "Esa anécdota resume buena parte de lo que encontré durante el trabajo de campo: recursos que existen "
    "pero no circulan, docentes con voluntad pero sin tiempo. Entrevisté a once maestras. Ocho dijeron lo mismo."
)


def fake_provider(query, limit=5):
    return [{"source": "OpenAlex", "title": "IA en educación superior",
             "abstract": "los docentes deben adaptarse a un entorno cada vez más dinámico y exigente",
             "url": "https://doi.org/10.0000/demo", "year": 2023, "type": "article", "venue": "Demo", "authors": ["A. B."]}]


def client():
    app_module.app.config["TESTING"] = True
    for k in list(plagiarism.PROVIDERS):
        plagiarism.PROVIDERS[k] = fake_provider
    return app_module.app.test_client()


def test_sentence_offsets_are_exact():
    text = "Primera oración, dice el Dr. Pérez. Segunda oración aquí!\n\nTercera en otro párrafo."
    sents = split_sentences(text)
    assert [s.text for s in sents] == ["Primera oración, dice el Dr. Pérez.", "Segunda oración aquí!",
                                       "Tercera en otro párrafo."]
    assert all(text[s.start:s.end] == s.text for s in sents)
    assert sents[2].paragraph == 1


def test_audit_scores_ai_text_higher_than_human():
    c = client()
    a = c.post("/api/v1/audit", json={"text": AI_TEXT, "use_gemini": False, "check_plagiarism": False}).get_json()
    h = c.post("/api/v1/audit", json={"text": HUMAN_TEXT, "use_gemini": False, "check_plagiarism": False}).get_json()
    assert a["ok"] and h["ok"]
    assert a["summary"]["ai_probability"] > h["summary"]["ai_probability"]


def test_plagiarism_detects_copied_fragment():
    c = client()
    r = c.post("/api/v1/audit", json={"text": AI_TEXT, "use_gemini": False, "providers": ["openalex"]}).get_json()
    assert r["plagiarism"]["available"]
    assert r["plagiarism"]["similarity_index"] > 0
    assert any(s["flag"] == "plagio" for s in r["sentences"])


def test_rewrite_local_and_export_docx():
    c = client()
    rw = c.post("/api/v1/rewrite", json={"text": AI_TEXT, "institution": "upel"}).get_json()
    assert rw["ok"] and rw["engine"] == "local-rules" and rw["rewrites"]
    assert "(García, 2021)" in rw["corrected_text"]
    res = c.post("/api/v1/export", json={"text": rw["corrected_text"], "institution": "upel", "title": "Prueba",
                                         "report": {"ai": {"ai_probability": 50}, "plagiarism": {}},
                                         "rewrites": rw["rewrites"]})
    assert res.status_code == 200 and res.data[:2] == b"PK"


def test_upload_docx_extract():
    from docx import Document
    buf = io.BytesIO(); d = Document(); d.add_paragraph(HUMAN_TEXT); d.save(buf); buf.seek(0)
    r = client().post("/api/v1/extract", data={"file": (buf, "t.docx")}, content_type="multipart/form-data")
    assert r.get_json()["text"].startswith("Llegué")


def test_validation_and_auth():
    c = client()
    assert c.post("/api/v1/audit", json={"text": "corto"}).status_code == 422
    app_module.API_KEYS.add("secret")
    try:
        assert c.post("/api/v1/audit", json={"text": AI_TEXT}).status_code == 401
        ok = c.post("/api/v1/audit", json={"text": AI_TEXT, "use_gemini": False, "check_plagiarism": False},
                    headers={"X-API-Key": "secret"})
        assert ok.status_code == 200
    finally:
        app_module.API_KEYS.clear()


# ---------------------------------------------------------------- reescritura por lotes
def _segments(n):
    sents = split_sentences(AI_TEXT)
    return [{"index": i, "text": sents[i % len(sents)].text, "before": "", "after": ""} for i in range(n)]


def test_rewrite_segments_mode_and_limits():
    c = client()
    ok = c.post("/api/v1/rewrite", json={"segments": _segments(3), "institution": "upel"}).get_json()
    assert ok["ok"] and ok["processed"] == [0, 1, 2] and ok["done"] and "corrected_text" not in ok
    too_many = c.post("/api/v1/rewrite", json={"segments": _segments(app_module.REWRITE_MAX_PER_REQUEST + 1)})
    assert too_many.status_code == 413 and too_many.get_json()["max_per_request"] == app_module.REWRITE_MAX_PER_REQUEST
    bad = c.post("/api/v1/rewrite", json={"segments": [{"index": "x", "text": "hola"}]})
    assert bad.status_code == 400 and bad.get_json()["ok"] is False
    assert c.post("/api/v1/rewrite", data="no-json", content_type="text/plain").status_code == 400


def test_rewrite_index_mode_paginates():
    long_text = " ".join([AI_TEXT] * 3)
    r = client().post("/api/v1/rewrite", json={"text": long_text, "indices": list(range(12))}).get_json()
    assert len(r["processed"]) == app_module.REWRITE_MAX_PER_REQUEST
    assert r["pending"] == list(range(app_module.REWRITE_MAX_PER_REQUEST, 12)) and r["done"] is False


def test_rewrite_gemini_rate_limit_returns_429(monkeypatch=None):
    from services.gemini_client import GeminiRateLimitError
    c = client()
    gem = app_module.gemini
    old_key, old_fn = gem.api_key, gem.generate_json
    gem.api_key = "x"
    gem.generate_json = lambda *a, **k: (_ for _ in ()).throw(GeminiRateLimitError("429", retry_after=17))
    try:
        r = c.post("/api/v1/rewrite", json={"segments": _segments(2)})
        assert r.status_code == 429 and r.headers["Retry-After"] == "17"
        assert r.get_json()["retryable"] is True
    finally:
        gem.api_key, gem.generate_json = old_key, old_fn


def test_rewrite_gemini_timeout_falls_back_to_local():
    from services.gemini_client import GeminiTimeoutError
    c = client()
    gem = app_module.gemini
    old_key, old_fn = gem.api_key, gem.generate_json
    gem.api_key = "x"
    gem.generate_json = lambda *a, **k: (_ for _ in ()).throw(GeminiTimeoutError("lento"))
    try:
        r = c.post("/api/v1/rewrite", json={"segments": _segments(3)})
        body = r.get_json()
        assert r.status_code == 200 and body["engine"] == "local-rules" and "motor local" in body["warning"]
    finally:
        gem.api_key, gem.generate_json = old_key, old_fn


def test_rewrite_unexpected_error_is_json_500():
    c = client()
    original = app_module.rewrite_segments
    app_module.rewrite_segments = lambda *a, **k: 1 / 0
    try:
        r = c.post("/api/v1/rewrite", json={"segments": _segments(1)})
        assert r.status_code == 500 and r.is_json and r.get_json()["retryable"] is True
    finally:
        app_module.rewrite_segments = original


def test_gemini_client_budget_and_429_parsing():
    import time as _t
    from services.gemini_client import GeminiClient, GeminiRateLimitError, GeminiTimeoutError

    class Resp:
        def __init__(self, code, body=None):
            self.status_code, self._b, self.text, self.headers = code, body or {}, "", {}
        def json(self):
            return self._b

    g429 = GeminiClient(api_key="k")
    g429._session.post = lambda *a, **k: Resp(429, {"error": {"details": [{"retryDelay": "31s"}]}})
    try:
        g429.generate_json("s", "p", budget=10); assert False
    except GeminiRateLimitError as e:
        assert e.retry_after == 31          # no espera 31 s dentro de una petición de 10 s

    gslow = GeminiClient(api_key="k")
    def slow(*a, **k):
        _t.sleep(0.01)
        import requests; raise requests.Timeout()
    gslow._session.post = slow
    t0 = _t.monotonic()
    try:
        gslow.generate_json("s", "p", budget=4, retries=3); assert False
    except GeminiTimeoutError:
        assert _t.monotonic() - t0 < 4


# ---------------------------------------------------------------- proyectos / borradores
CID = {"X-Client-Id": "test-client-0000000001"}
OTHER = {"X-Client-Id": "otro-cliente-000000002"}


def _project(**over):
    p = {"status": "reescribiendo", "title": "Tesis demo",
         "context": {"institucion_id": "UDC", "docente_id": "DOC-1", "periodo": "Periodo I",
                     "área/asignatura": "Metodología"},
         "document": {"text": AI_TEXT, "norma": "upel"},
         "report": {"summary": {"ai_probability": 61}, "institution": {"id": "upel"}},
         "rewrite": {"items": {"0": {"status": "reescrito", "original": "a", "rewritten": "b"},
                               "3": {"status": "pendiente"}}}}
    p.update(over)
    return p


def test_project_save_load_update_and_conflict():
    c = client()
    r = c.post("/api/v1/projects/save", json=_project(), headers=CID)
    assert r.status_code == 201
    meta = r.get_json()["project"]; pid = meta["id"]; assert meta["revision"] == 1
    loaded = c.get(f"/api/v1/projects/load?id={pid}", headers=CID).get_json()["project"]
    assert loaded["context"] == {"institucion_id": "UDC", "docente_id": "DOC-1", "periodo": "Periodo I",
                                 "area_asignatura": "Metodología"}
    assert loaded["rewrite"]["items"]["3"]["status"] == "pendiente"
    assert loaded["document"]["text"] == AI_TEXT
    up = c.post("/api/v1/projects/save", json=_project(id=pid, revision=1, status="pausado"), headers=CID)
    assert up.status_code == 200 and up.get_json()["project"]["revision"] == 2
    stale = c.post("/api/v1/projects/save", json=_project(id=pid, revision=1), headers=CID)
    assert stale.status_code == 409
    lst = c.get("/api/v1/projects?area_asignatura=Metodología&periodo=Periodo I", headers=CID).get_json()["projects"]
    assert lst[0]["id"] == pid and lst[0]["status"] == "pausado"


def test_project_ownership_and_validation():
    c = client()
    pid = c.post("/api/v1/projects/save", json=_project(), headers=CID).get_json()["project"]["id"]
    assert c.get(f"/api/v1/projects/load?id={pid}", headers=OTHER).status_code == 404
    assert c.post("/api/v1/projects/save", json=_project(id=pid), headers=OTHER).status_code == 404
    assert c.post("/api/v1/projects/save", json=_project(), headers={}).status_code == 400   # sin X-Client-Id
    assert c.post("/api/v1/projects/save", json=_project(status="raro"), headers=CID).status_code == 400
    assert c.post("/api/v1/projects/save", json=_project(id="no-uuid"), headers=CID).status_code == 400
    assert c.delete(f"/api/v1/projects/{pid}", headers=CID).get_json()["ok"]
    assert c.get(f"/api/v1/projects/load?id={pid}", headers=CID).status_code == 404


def test_sqlite_migration_from_old_schema(tmp_path=None):
    import sqlite3, tempfile
    from services.projects import SQLiteProjectStore
    path = os.path.join(tempfile.mkdtemp(), "old.sqlite3")
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE projects (id TEXT PRIMARY KEY, owner TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
        status TEXT NOT NULL DEFAULT 'borrador', title TEXT, norma TEXT, institucion_id TEXT, docente_id TEXT,
        asignatura TEXT, grado TEXT, estudiante_nombre TEXT, state_json TEXT NOT NULL, created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL)""")
    con.execute("INSERT INTO projects VALUES ('11111111-1111-1111-1111-111111111111','o',1,'borrador','t',NULL,"
                "'U','D','Historia',NULL,NULL,'{}','x','x')")
    con.commit(); con.close()
    store = SQLiteProjectStore(path)
    assert store.list("o")[0]["area_asignatura"] == "Historia"


# ---------------------------------------------------------------- multi-LLM (Gemini / Claude)
import types  # noqa: E402


def _install_fake_anthropic(behavior):
    """SDK 'anthropic' simulado: behavior(kwargs) devuelve texto o lanza una excepción del SDK."""
    sdk = types.ModuleType("anthropic")

    class APIError(Exception): pass
    class APIStatusError(APIError):
        def __init__(self, status_code=500, headers=None):
            super().__init__(f"HTTP {status_code}")
            self.status_code = status_code
            self.response = types.SimpleNamespace(headers=headers or {})
    class RateLimitError(APIStatusError): pass
    class AuthenticationError(APIStatusError): pass
    class PermissionDeniedError(APIStatusError): pass
    class NotFoundError(APIStatusError): pass
    class APIConnectionError(APIError): pass
    class APITimeoutError(APIConnectionError): pass
    for cls in (APIError, APIStatusError, RateLimitError, AuthenticationError, PermissionDeniedError,
                NotFoundError, APIConnectionError, APITimeoutError):
        setattr(sdk, cls.__name__, cls)

    calls = []
    class _Messages:
        def create(self, **kw):
            calls.append(kw)
            text = behavior(kw, sdk)
            return types.SimpleNamespace(stop_reason="end_turn", content=[
                types.SimpleNamespace(type="thinking", thinking="..."), types.SimpleNamespace(type="text", text=text)])
    class Anthropic:
        def __init__(self, **kw): self.messages = _Messages()
        def with_options(self, **kw): return self
    sdk.Anthropic = Anthropic
    sys.modules["anthropic"] = sdk
    return calls


def _with_engines(gemini_fn=None, claude_key=""):
    gem, cl = app_module.gemini, app_module.claude
    saved = (gem.api_key, gem.generate_json, cl.api_key, cl._client)
    gem.api_key = "g" if gemini_fn else ""
    if gemini_fn:
        gem.generate_json = gemini_fn
    cl.api_key, cl._client = claude_key, None
    def restore():
        gem.api_key, gem.generate_json, cl.api_key, cl._client = saved
        sys.modules.pop("anthropic", None)
    return restore


def _gemini_rewrite(system, prompt, **kw):
    import json as _j
    items = _j.loads(prompt.split("ELEMENTOS:\n", 1)[1])
    return [{"index": it["index"], "rewritten": "[G] " + it["text"], "changes": []} for it in items]


def test_claude_without_key_falls_back_to_gemini_with_notice():
    restore = _with_engines(gemini_fn=_gemini_rewrite, claude_key="")
    try:
        r = client().post("/api/v1/rewrite", json={"segments": _segments(2), "provider": "claude"}).get_json()
        assert r["llm"]["used"] == "gemini" and r["llm"]["requested"] == "claude"
        assert r["llm"]["notice"].startswith("El motor Claude no está configurado")
        assert r["engine"].startswith("gemini:") and r["rewrites"][0]["rewritten"].startswith("[G]")
    finally:
        restore()


def test_claude_rewrite_and_audit_with_sdk():
    import json as _j
    def behavior(kw, sdk):
        assert "temperature" not in kw and kw["thinking"] == {"type": "disabled"}   # Sonnet 5
        prompt = kw["messages"][0]["content"]
        if "flagged_sentences" in prompt:
            return _j.dumps({"ai_probability": 80, "predictability": 70, "verdict": "ia",
                             "robotic_phrases": [], "flagged_sentences": [], "rationale": "ok"})
        items = _j.loads(prompt.split("ELEMENTOS:\n", 1)[1])
        return "```json\n" + _j.dumps([{"index": i["index"], "rewritten": "[C] " + i["text"]} for i in items]) + "\n```"
    calls = _install_fake_anthropic(behavior)
    restore = _with_engines(gemini_fn=None, claude_key="sk-test")
    try:
        c = client()
        r = c.post("/api/v1/rewrite", json={"segments": _segments(3), "provider": "claude"}).get_json()
        assert r["engine"] == "claude:claude-sonnet-5" and r["llm"]["notice"] is None
        assert all(w["rewritten"].startswith("[C]") for w in r["rewrites"])
        a = c.post("/api/v1/audit", json={"text": AI_TEXT, "provider": "claude", "check_plagiarism": False}).get_json()
        assert a["ai"]["semantic"]["available"] and a["ai"]["semantic"]["provider"] == "claude"
        assert a["llm"]["used"] == "claude" and len(calls) == 2
    finally:
        restore()


def test_claude_quota_returns_neutral_429():
    def behavior(kw, sdk):
        raise sdk.RateLimitError(429, headers={"retry-after": "40"})
    _install_fake_anthropic(behavior)
    restore = _with_engines(gemini_fn=None, claude_key="sk-test")
    try:
        r = client().post("/api/v1/rewrite", json={"segments": _segments(2), "provider": "claude"})
        body = r.get_json()
        assert r.status_code == 429 and r.headers["Retry-After"] == "40"
        assert body["error"].startswith("Cuota de uso excedida temporalmente")
        assert "Gemini" not in body["error"] and "Claude" not in body["error"]
    finally:
        restore()


def test_gemini_quota_message_is_neutral():
    from services.gemini_client import GeminiRateLimitError
    restore = _with_engines(gemini_fn=lambda *a, **k: (_ for _ in ()).throw(GeminiRateLimitError(retry_after=9)))
    try:
        r = client().post("/api/v1/rewrite", json={"segments": _segments(1)})
        assert r.status_code == 429 and "Gemini" not in r.get_json()["error"]
    finally:
        restore()


def test_claude_invalid_key_at_runtime_switches_to_gemini():
    def behavior(kw, sdk):
        raise sdk.AuthenticationError(401)
    _install_fake_anthropic(behavior)
    restore = _with_engines(gemini_fn=_gemini_rewrite, claude_key="sk-revocada")
    try:
        r = client().post("/api/v1/rewrite", json={"segments": _segments(2), "provider": "claude"}).get_json()
        assert r["llm"]["used"] == "gemini" and r["llm"]["notice"].startswith("El motor Claude")
        assert r["rewrites"][0]["rewritten"].startswith("[G]")
    finally:
        restore()
