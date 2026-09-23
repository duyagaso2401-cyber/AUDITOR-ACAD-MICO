# Auditor Académico

Plataforma web (Flask) para auditar trabajos académicos: **detección de texto generado por IA**, **similitud con literatura científica abierta** y **reescritura con estilo institucional** (UPEL, APA 7 – Universidad de Cartagena/Colombia, IEEE, Vancouver), con exportación a Word (.docx) y API REST.

```
┌──────────── Dashboard (HTML/JS) ────────────┐        ┌──── Integraciones externas ────┐
│ medidores · mapa de texto · fuentes · .docx │        │   POST /api/v1/audit (X-API-Key)│
└───────────────────┬─────────────────────────┘        └───────────────┬────────────────┘
                    ▼                                                  ▼
              app.py (Flask · rate-limit · auth · CORS · errores JSON · ProxyFix)
                    │
     ┌──────────────┼──────────────────┬────────────────────┬──────────────────┐
     ▼              ▼                  ▼                    ▼                  ▼
ai_detector     plagiarism          rewriter            documents        institutions
 ├ burstiness    ├ OpenAlex          ├ Gemini (estilo)    ├ .docx/.pdf in   UPEL · APA 7
 ├ perplejidad   ├ Crossref          └ reglas locales     └ .docx out       IEEE · Vancouver
 ├ muletillas    ├ Semantic Scholar
 └ Gemini 2.5    └ Scholar/Web (Serper)
```

## Motores

| Motor | Qué hace | Cómo |
|---|---|---|
| **IA – matemático** | Burstiness (CV y B de Goh‑Barabási de la longitud de oraciones), perplejidad estimada con un bigrama interpolado *leave‑one‑sentence‑out* y su dispersión, densidad de muletillas de LLM, uniformidad estructural, MATTR. | Local, sin red, determinista. |
| **IA – semántico** | Predictibilidad del discurso, fórmulas robóticas, genericidad, marcación por oración. | Gemini 2.5 (REST, salida JSON). Fusión 40 % local / 60 % semántico. |
| **Similitud** | Selecciona los fragmentos más distintivos, los consulta en paralelo y compara por trigramas (copia literal) y coseno TF (paráfrasis). | OpenAlex, Crossref, Semantic Scholar y, con `SERPER_API_KEY`, Google Scholar + búsqueda web de frase exacta. |
| **Reescritura** | Paráfrasis con fluidez humana y variabilidad sintáctica según la norma elegida; protege citas y cifras (rechaza la propuesta si las pierde). | Gemini con guía de estilo institucional; respaldo local por reglas. |
| **Exportación** | .docx con márgenes, fuente, interlineado y sangría de la institución + anexo con informe y registro de cambios. | python-docx. |

## Ejecución local

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                   # complete GEMINI_API_KEY, OPENALEX_API_KEY…
python app.py                                          # http://localhost:5000
python -m pytest -q                                    # pruebas (pip install pytest)
```

## Variables de entorno

| Variable | Obligatoria | Descripción |
|---|---|---|
| `SECRET_KEY` | Sí (prod) | Firma de sesión. Render la genera con `render.yaml`. |
| `GEMINI_API_KEY` | Recomendada | Activa la capa semántica y la reescritura completa ([AI Studio](https://aistudio.google.com/apikey)). |
| `GEMINI_MODEL` | No | `gemini-2.5-flash` (defecto) o `gemini-2.5-pro`. |
| `GEMINI_FALLBACK_MODELS` | No | Modelos de respaldo si la cuenta no tiene acceso a 2.5 (defecto `gemini-3.5-flash`). |
| `OPENALEX_API_KEY` | Recomendada | Desde feb‑2026 OpenAlex exige clave (gratuita, 100 000 créditos/día). Sin ella sólo hay 100 créditos/día. |
| `SERPER_API_KEY` | No | Google Scholar y búsqueda web de frase exacta (repositorios institucionales). |
| `SEMANTIC_SCHOLAR_API_KEY` | No | Mayor cuota en Semantic Scholar. |
| `CONTACT_EMAIL` | No | Se envía a Crossref (polite pool). |
| `API_KEYS` | Sí (prod) | Claves para `/api/v1/*` separadas por coma. Vacía = API abierta. El dashboard usa sesión. |
| `CORS_ORIGINS` | No | Orígenes permitidos para integraciones desde navegador (`*` o lista). |
| `RATE_LIMIT_PER_MIN`, `MAX_TEXT_CHARS`, `MAX_UPLOAD_MB` | No | Límites operativos. |

## Despliegue: GitHub → Render

1. `git init && git add . && git commit -m "Auditor Académico v1"` y súbalo a un repositorio de GitHub.
2. En Render: **New + → Blueprint** → elija el repositorio (usa `render.yaml`).
   Alternativa: **New + → Web Service**, build `pip install -r requirements.txt`; Render detecta el `Procfile`.
3. Complete en *Environment* las variables marcadas `sync: false` (`GEMINI_API_KEY`, `OPENALEX_API_KEY`, `API_KEYS`…).
4. Health check: `GET /health`. Cada `git push` a la rama principal redepliega.

> El rate‑limit y la caché son en memoria (por instancia). Para varias instancias, reemplácelos por Render Key Value (Redis).

## API REST

`POST /api/v1/audit` — JSON o `multipart/form-data` (campo `file`).

```bash
curl -X POST https://SU-APP.onrender.com/api/v1/audit \
  -H "Content-Type: application/json" -H "X-API-Key: SU_CLAVE" \
  -d '{"text":"…","institution":"upel","use_gemini":true,"check_plagiarism":true,
       "providers":["openalex","crossref"],"max_fragments":8,"rewrite":true,"mode":"fluido"}'
```

Respuesta (resumen):

```json
{
  "ok": true,
  "summary": {"ai_probability": 62.4, "similarity_index": 14.8, "integrity_score": 50.6,
              "verdict": "REVISIÓN OBLIGATORIA – …", "flagged_sentences": 9},
  "ai": {"local": {"metrics": {"burstiness_cv": 0.21, "perplexity_estimated": 188.2, "…": "…"}},
         "semantic": {"ai_probability": 71, "rationale": "…"}, "per_sentence": ["…"]},
  "plagiarism": {"sources": [{"title": "…", "url": "…", "max_similarity": 48.1}], "fragments": ["…"]},
  "sentences": [{"index": 0, "start": 0, "end": 120, "ai_score": 55.0, "flag": "ia_media"}],
  "rewrite": {"engine": "gemini:gemini-2.5-flash", "rewrites": ["…"], "corrected_text": "…"}
}
```

**Reescritura por lotes** — `POST /api/v1/rewrite` procesa como máximo `REWRITE_MAX_PER_REQUEST` (5) oraciones por petición y responde en ≤ `REWRITE_TIME_BUDGET` (20 s):

```json
{"segments": [{"index": 12, "text": "…", "before": "…", "after": "…"}], "institution": "upel", "mode": "fluido"}
```

También acepta `{"text": "…", "indices": [..]}`: procesa los primeros 5 y devuelve `"pending"` con los que faltan. Códigos: `400` petición inválida, `413` demasiados segmentos, `429` cuota de Gemini (con `Retry-After`; reintente ese lote), `500` error inesperado (JSON con `request_id`). Si Gemini tarda demasiado o rechaza la clave, el lote se resuelve con el motor local y la respuesta incluye `warning`.

Otros endpoints: `POST /api/v1/export` (devuelve .docx), `POST /api/v1/extract`, `GET /api/v1/institutions`, `GET /health`. Añada `"compact": true` a `/audit` para respuestas livianas.

Para añadir una universidad, agregue un `Institution` en `services/institutions.py` (guía de estilo, formato de página y umbrales).

## Alcance y uso responsable

- **La detección de IA es probabilística.** Ningún detector (tampoco Turnitin o GPTZero) es infalible; textos humanos muy formales o de autores no nativos pueden obtener puntuaciones altas. Use el resultado como indicio para una revisión humana, nunca como prueba única.
- **El índice de similitud es muestral**: las APIs abiertas indexan títulos y resúmenes, no el texto completo de cada obra ni repositorios privados de trabajos estudiantiles como el de Turnitin.
- **La reescritura mejora la redacción, no reemplaza la citación.** Parafrasear ideas ajenas sigue exigiendo citar la fuente; revise las políticas de su institución sobre el uso de IA.
