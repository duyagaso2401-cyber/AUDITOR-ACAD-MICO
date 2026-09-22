"""
Cliente mínimo para la API REST de Google Gemini (Generative Language API).

Se usa REST directo en vez del SDK para reducir dependencias y tener control
explícito de timeouts y reintentos en Render.

Variables de entorno:
    GEMINI_API_KEY   clave de Google AI Studio (obligatoria para el modo semántico)
    GEMINI_MODEL     por defecto ``gemini-2.5-flash`` (use ``gemini-2.5-pro`` para mayor rigor)
    GEMINI_FALLBACK_MODELS  modelos alternativos separados por coma si el principal
                     devuelve 404 (Google limita Gemini 2.5 a cuentas que ya lo
                     usaban). Por defecto ``gemini-3.5-flash``.
    GEMINI_TIMEOUT   segundos, por defecto 45
"""
from __future__ import annotations

import json
import logging
import os
import re
import time

import requests

log = logging.getLogger(__name__)

API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"


class GeminiError(RuntimeError):
    pass


class GeminiClient:
    def __init__(self, api_key: str | None = None, model: str | None = None, timeout: float | None = None):
        self.api_key = api_key if api_key is not None else os.getenv("GEMINI_API_KEY", "")
        self.model = model or os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
        self.fallbacks = [m.strip() for m in os.getenv("GEMINI_FALLBACK_MODELS", "gemini-3.5-flash").split(",")
                          if m.strip() and m.strip() != self.model]
        self.timeout = timeout or float(os.getenv("GEMINI_TIMEOUT", "45"))
        self._session = requests.Session()

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def generate_json(self, system: str, prompt: str, temperature: float = 0.2,
                      max_output_tokens: int = 8192, retries: int = 2) -> dict | list:
        """Llama a Gemini forzando salida JSON y la devuelve ya parseada.
        Si el modelo configurado no existe para la cuenta (404), prueba los de respaldo
        y recuerda el que funcione."""
        if not self.enabled:
            raise GeminiError("GEMINI_API_KEY no configurada")
        for model in [self.model] + self.fallbacks:
            try:
                result = self._call(model, system, prompt, temperature, max_output_tokens, retries)
                if model != self.model:
                    log.warning("Gemini: '%s' no disponible; se usa '%s'", self.model, model)
                    self.fallbacks = [m for m in self.fallbacks if m != model] + [self.model]
                    self.model = model
                return result
            except _ModelNotFound:
                continue
        raise GeminiError("Ningún modelo Gemini configurado está disponible para esta clave")

    def _call(self, model, system, prompt, temperature, max_output_tokens, retries):
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_output_tokens,
                "responseMimeType": "application/json",
            },
        }
        # Gemini 2.5 Flash permite desactivar el "thinking" para bajar la latencia.
        if model.startswith("gemini-2.5-flash"):
            body["generationConfig"]["thinkingConfig"] = {"thinkingBudget": 0}

        url = f"{API_ROOT}/{model}:generateContent"
        headers = {"x-goog-api-key": self.api_key, "Content-Type": "application/json"}
        last_err: Exception | None = None
        for attempt in range(retries + 1):
            try:
                resp = self._session.post(url, headers=headers, json=body, timeout=self.timeout)
                if resp.status_code == 404:
                    raise _ModelNotFound(model)
                if resp.status_code in (429, 500, 502, 503, 504):
                    raise _Retryable(f"HTTP {resp.status_code}: {resp.text[:200]}")
                if resp.status_code >= 400:
                    # 400/401/403 (clave inválida, prompt bloqueado): no se reintenta.
                    raise GeminiError(f"HTTP {resp.status_code}: {resp.text[:300]}")
                return _parse_json(_extract_text(resp.json()))
            except (requests.RequestException, _Retryable, ValueError) as exc:
                last_err = exc
                if attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
        log.warning("Gemini falló: %s", last_err)
        raise GeminiError(str(last_err))


class _ModelNotFound(Exception):
    pass


class _Retryable(Exception):
    pass


def _extract_text(payload: dict) -> str:
    candidates = payload.get("candidates") or []
    if not candidates:
        reason = payload.get("promptFeedback", {}).get("blockReason", "sin candidatos")
        raise GeminiError(f"Respuesta vacía de Gemini ({reason})")
    parts = candidates[0].get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    if not text:
        raise GeminiError("Gemini no devolvió texto")
    return text


def _parse_json(text: str):
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"(\{.*\}|\[.*\])", text, re.S)
        if m:
            return json.loads(m.group(1))
        raise ValueError("JSON inválido devuelto por Gemini")
