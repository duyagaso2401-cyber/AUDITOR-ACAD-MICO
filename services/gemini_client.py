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
    GEMINI_TIMEOUT   segundos por intento, por defecto 25
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
    """Error genérico de Gemini (respuesta inválida, prompt bloqueado, etc.)."""


class GeminiRateLimitError(GeminiError):
    """Cuota agotada (HTTP 429). ``retry_after`` = segundos sugeridos por Google."""

    def __init__(self, message: str, retry_after: float = 15.0):
        super().__init__(message)
        self.retry_after = retry_after


class GeminiTimeoutError(GeminiError):
    """La llamada no terminó dentro del presupuesto de tiempo de la petición."""


class GeminiAuthError(GeminiError):
    """Clave inválida o sin permisos (HTTP 400 por API key / 401 / 403)."""


class GeminiClient:
    def __init__(self, api_key: str | None = None, model: str | None = None, timeout: float | None = None):
        self.api_key = api_key if api_key is not None else os.getenv("GEMINI_API_KEY", "")
        self.model = model or os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
        self.fallbacks = [m.strip() for m in os.getenv("GEMINI_FALLBACK_MODELS", "gemini-3.5-flash").split(",")
                          if m.strip() and m.strip() != self.model]
        self.timeout = timeout or float(os.getenv("GEMINI_TIMEOUT", "25"))
        self._session = requests.Session()

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def generate_json(self, system: str, prompt: str, temperature: float = 0.2,
                      max_output_tokens: int = 8192, retries: int = 1,
                      budget: float | None = None) -> dict | list:
        """Llama a Gemini forzando salida JSON y la devuelve ya parseada.

        ``budget``: segundos máximos que puede consumir TODA la operación (intentos,
        esperas y modelos de respaldo). Garantiza que la petición HTTP de Flask responda
        antes del timeout del proxy de Render. Si se agota lanza ``GeminiTimeoutError``.
        Si el modelo configurado no existe para la cuenta (404), prueba los de respaldo.
        """
        if not self.enabled:
            raise GeminiError("GEMINI_API_KEY no configurada")
        deadline = time.monotonic() + (budget if budget else self.timeout * (retries + 1) + 5)
        for model in [self.model] + self.fallbacks:
            try:
                result = self._call(model, system, prompt, temperature, max_output_tokens, retries, deadline)
                if model != self.model:
                    log.warning("Gemini: '%s' no disponible; se usa '%s'", self.model, model)
                    self.fallbacks = [m for m in self.fallbacks if m != model] + [self.model]
                    self.model = model
                return result
            except _ModelNotFound:
                continue
        raise GeminiError("Ningún modelo Gemini configurado está disponible para esta clave")

    def _call(self, model, system, prompt, temperature, max_output_tokens, retries, deadline):
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
            remaining = deadline - time.monotonic()
            if remaining < 3:
                raise GeminiTimeoutError("Se agotó el tiempo disponible para Gemini")
            try:
                resp = self._session.post(url, headers=headers, json=body,
                                          timeout=(5, min(self.timeout, remaining)))
            except requests.Timeout as exc:
                last_err = GeminiTimeoutError(f"Gemini no respondió a tiempo ({exc.__class__.__name__})")
                continue
            except requests.RequestException as exc:
                last_err = GeminiError(f"Error de red con Gemini: {exc}")
                _sleep_within(deadline, 1.5 * (attempt + 1))
                continue

            status = resp.status_code
            if status == 404:
                raise _ModelNotFound(model)
            if status == 429:
                wait = _retry_delay(resp)
                # Sólo se espera dentro de la petición si cabe en el presupuesto;
                # si no, se devuelve el 429 al cliente para que reintente él.
                if attempt < retries and wait + 3 < deadline - time.monotonic():
                    time.sleep(wait)
                    continue
                raise GeminiRateLimitError("Cuota de Gemini excedida (429)", retry_after=wait)
            if status in (401, 403) or (status == 400 and "API_KEY" in resp.text.upper()):
                raise GeminiAuthError(f"Gemini rechazó la clave (HTTP {status})")
            if status in (500, 502, 503, 504):
                last_err = GeminiError(f"Gemini no disponible (HTTP {status})")
                _sleep_within(deadline, 1.5 * (attempt + 1))
                continue
            if status >= 400:
                raise GeminiError(f"HTTP {status}: {resp.text[:300]}")
            try:
                return _parse_json(_extract_text(resp.json()))
            except ValueError as exc:  # JSON truncado o mal formado: se reintenta
                last_err = GeminiError(str(exc))
        log.warning("Gemini falló: %s", last_err)
        raise last_err if isinstance(last_err, GeminiError) else GeminiError(str(last_err))


def _sleep_within(deadline: float, seconds: float) -> None:
    time.sleep(max(0.0, min(seconds, deadline - time.monotonic() - 3)))


def _retry_delay(resp) -> float:
    """Extrae el retryDelay que Google incluye en el cuerpo de un 429 (p. ej. "17s")."""
    header = resp.headers.get("Retry-After") if hasattr(resp, "headers") and resp.headers else None
    if header and header.isdigit():
        return float(header)
    try:
        for detail in resp.json().get("error", {}).get("details", []):
            delay = detail.get("retryDelay")
            if delay:
                return float(str(delay).rstrip("s"))
    except Exception:  # noqa: BLE001
        pass
    return 15.0


class _ModelNotFound(Exception):
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
