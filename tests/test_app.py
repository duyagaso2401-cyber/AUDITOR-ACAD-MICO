"""Pruebas de humo: ejecutar con  `python -m pytest -q`  (sin red: se simulan las APIs externas)."""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("GEMINI_API_KEY", "")

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
