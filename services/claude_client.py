"""
Cliente de Anthropic Claude con la librería oficial ``anthropic``.

Variables de entorno:
    ANTHROPIC_API_KEY   clave de https://console.anthropic.com (obligatoria para usar Claude)
    CLAUDE_MODEL        por defecto ``claude-sonnet-5`` (equilibrio velocidad/calidad).
                        Alternativas: ``claude-haiku-4-5-20251001`` (más rápido y económico),
                        ``claude-opus-5-5`` (máxima calidad, más lento).
    CLAUDE_TIMEOUT      segundos por intento, por defecto 25

Notas de compatibilidad (Claude Sonnet 5 y posteriores):
    * No aceptan ``temperature``/``top_p``/``top_k`` distintos del valor por defecto (400),
      por eso este cliente nunca los envía.
    * No admiten "prefill" del mensaje del asistente: el formato JSON se pide por
      instrucciones de sistema y se valida al recibir la respuesta.
    * El razonamiento adaptativo viene activado por defecto; aquí se desactiva en Sonnet 5
      para que cada lote responda en pocos segundos (límite de tiempo de Render).
    * La respuesta puede traer bloques de razonamiento antes del texto: se filtran por tipo.
"""
from __future__ import annotations

import logging
import os
import time

from .llm import (QUOTA_MESSAGE, LLMAuthError, LLMError, LLMRateLimitError, LLMTimeoutError,
                  parse_json_text)

log = logging.getLogger(__name__)

_JSON_RULE = ("\n\nFORMATO DE SALIDA: responde únicamente con JSON válido, sin texto adicional "
              "ni bloques de código.")


def _sdk():
    """Importación diferida: la app arranca aunque la librería no esté instalada."""
    try:
        import anthropic  # noqa: PLC0415
        return anthropic
    except ImportError:
        return None


class ClaudeClient:
    name = "claude"
    label = "Anthropic Claude"

    def __init__(self, api_key: str | None = None, model: str | None = None, timeout: float | None = None):
        self.api_key = api_key if api_key is not None else os.getenv("ANTHROPIC_API_KEY", "")
        self.model = model or os.getenv("CLAUDE_MODEL", "claude-sonnet-5")
        self.timeout = timeout or float(os.getenv("CLAUDE_TIMEOUT", "25"))
        self._client = None

    @property
    def enabled(self) -> bool:
        return bool(self.api_key) and _sdk() is not None

    def _get_client(self):
        if self._client is None:
            sdk = _sdk()
            if sdk is None:
                raise LLMAuthError("Librería 'anthropic' no instalada")
            # Los reintentos se gestionan aquí, dentro del presupuesto de tiempo.
            self._client = sdk.Anthropic(api_key=self.api_key, max_retries=0, timeout=self.timeout)
        return self._client

    def _request_kwargs(self, system: str, prompt: str, max_output_tokens: int) -> dict:
        kwargs = {
            "model": self.model,
            "max_tokens": max_output_tokens,
            "system": system + _JSON_RULE,
            "messages": [{"role": "user", "content": prompt}],
        }
        if self.model.startswith("claude-sonnet-5"):
            kwargs["thinking"] = {"type": "disabled"}     # latencia baja por lote
        return kwargs

    def generate_json(self, system: str, prompt: str, temperature: float = 0.2,  # noqa: ARG002
                      max_output_tokens: int = 8192, retries: int = 1,
                      budget: float | None = None):
        """Misma firma que GeminiClient.generate_json. ``temperature`` se ignora a propósito."""
        if not self.enabled:
            raise LLMAuthError("Motor Claude no configurado")
        sdk = _sdk()
        client = self._get_client()
        deadline = time.monotonic() + (budget if budget else self.timeout * (retries + 1) + 5)
        kwargs = self._request_kwargs(system, prompt, max_output_tokens)
        last_err: LLMError | None = None

        for attempt in range(retries + 1):
            remaining = deadline - time.monotonic()
            if remaining < 3:
                raise LLMTimeoutError("Se agotó el tiempo disponible para el motor de IA")
            try:
                resp = client.with_options(timeout=min(self.timeout, remaining)).messages.create(**kwargs)
            except sdk.RateLimitError as exc:
                wait = _retry_after(exc)
                if attempt < retries and wait + 3 < deadline - time.monotonic():
                    time.sleep(wait)
                    continue
                raise LLMRateLimitError(QUOTA_MESSAGE, retry_after=wait) from None
            except sdk.APITimeoutError:
                last_err = LLMTimeoutError("El motor de IA no respondió a tiempo")
                continue
            except (sdk.AuthenticationError, sdk.PermissionDeniedError):
                raise LLMAuthError("Clave del motor Claude inválida o sin permisos") from None
            except sdk.NotFoundError:
                raise LLMError(f"Modelo '{self.model}' no disponible para esta clave") from None
            except sdk.APIConnectionError as exc:
                last_err = LLMError(f"Error de red con el motor de IA: {exc.__class__.__name__}")
                _sleep_within(deadline, 1.5 * (attempt + 1))
                continue
            except sdk.APIStatusError as exc:
                status = getattr(exc, "status_code", 0)
                if status == 529:                        # servicio saturado: igual que cuota
                    raise LLMRateLimitError(QUOTA_MESSAGE, retry_after=_retry_after(exc, 20)) from None
                if status >= 500:
                    last_err = LLMError(f"Motor de IA no disponible (HTTP {status})")
                    _sleep_within(deadline, 1.5 * (attempt + 1))
                    continue
                raise LLMError(f"Solicitud rechazada por el motor de IA (HTTP {status})") from None

            text = "".join(getattr(b, "text", "") for b in (resp.content or []) if getattr(b, "type", "") == "text")
            if getattr(resp, "stop_reason", None) == "refusal" or not text:
                raise LLMError("El motor de IA no devolvió contenido")
            try:
                return parse_json_text(text)
            except ValueError as exc:                    # JSON truncado o inválido: se reintenta
                last_err = LLMError(str(exc))
        log.warning("Claude falló: %s", last_err)
        raise last_err or LLMError("Fallo desconocido del motor de IA")


def _retry_after(exc, default: float = 15.0) -> float:
    try:
        value = exc.response.headers.get("retry-after")
        return float(value) if value else default
    except Exception:  # noqa: BLE001
        return default


def _sleep_within(deadline: float, seconds: float) -> None:
    time.sleep(max(0.0, min(seconds, deadline - time.monotonic() - 3)))
