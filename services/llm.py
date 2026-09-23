"""
Capa común para motores de IA (multi-LLM: Google Gemini / Anthropic Claude).

Todos los clientes exponen la misma interfaz:
    name, label, model, enabled
    generate_json(system, prompt, temperature=0.2, max_output_tokens=..., retries=1, budget=None)

y lanzan las mismas excepciones, de modo que el detector, la reescritura y la API
no dependen del proveedor. Los mensajes de error visibles para el usuario son
neutros: nunca mencionan el nombre del proveedor.
"""
from __future__ import annotations

import json
import logging
import re

log = logging.getLogger(__name__)

QUOTA_MESSAGE = "Cuota de uso excedida temporalmente"
CLAUDE_NOT_CONFIGURED = ("El motor Claude no está configurado o no dispone de API Key activa. "
                         "Seleccionando Gemini por defecto.")
DEFAULT_PROVIDER = "gemini"
PROVIDER_ALIASES = {"gemini": "gemini", "google": "gemini", "claude": "claude", "anthropic": "claude"}


class LLMError(RuntimeError):
    """Error genérico del motor de IA (respuesta inválida, contenido bloqueado, etc.)."""


class LLMRateLimitError(LLMError):
    """Cuota agotada o servicio saturado. ``retry_after`` = segundos sugeridos."""

    def __init__(self, message: str = QUOTA_MESSAGE, retry_after: float = 15.0):
        super().__init__(message)
        self.retry_after = retry_after


class LLMTimeoutError(LLMError):
    """La llamada no terminó dentro del presupuesto de tiempo de la petición."""


class LLMAuthError(LLMError):
    """Clave inválida, revocada o sin permisos."""


def parse_json_text(text: str):
    """Extrae JSON de la respuesta del modelo (tolera bloques ```json y texto alrededor)."""
    text = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"(\{.*\}|\[.*\])", text, re.S)
        if m:
            return json.loads(m.group(1))
        raise ValueError("JSON inválido devuelto por el motor de IA")


def normalize_provider(value) -> str:
    return PROVIDER_ALIASES.get(str(value or "").strip().lower(), DEFAULT_PROVIDER)


class _FallbackLLM:
    """Envoltorio por petición: usa Claude y, si su clave resulta inválida en tiempo de
    ejecución, cambia a Gemini para el resto de la petición y deja un aviso."""

    def __init__(self, primary, secondary):
        self._active, self._secondary = primary, secondary
        self.notice: str | None = None

    def __getattr__(self, item):                       # name, label, model, enabled…
        return getattr(self._active, item)

    def generate_json(self, *args, **kwargs):
        try:
            return self._active.generate_json(*args, **kwargs)
        except LLMAuthError:
            if self._secondary is None or not self._secondary.enabled:
                raise
            log.warning("Clave de %s rechazada; se usa %s", self._active.name, self._secondary.name)
            self._active, self._secondary = self._secondary, None
            self.notice = CLAUDE_NOT_CONFIGURED
            return self._active.generate_json(*args, **kwargs)


class LLMRegistry:
    def __init__(self, clients: dict):
        self.clients = clients

    def status(self) -> dict:
        return {name: {"enabled": c.enabled, "label": c.label, "model": c.model}
                for name, c in self.clients.items()}

    def any_enabled(self) -> bool:
        return any(c.enabled for c in self.clients.values())

    def for_request(self, requested) -> tuple[object | None, str | None, str]:
        """Devuelve (cliente o None, aviso o None, proveedor solicitado normalizado).

        * Claude sin clave -> Gemini + aviso amigable.
        * Claude con clave -> envoltorio que cae a Gemini si la clave es rechazada.
        * Ningún motor configurado -> None (sólo métricas locales / reglas).
        """
        name = normalize_provider(requested)
        gemini, claude = self.clients.get("gemini"), self.clients.get("claude")
        if name == "claude":
            if claude is not None and claude.enabled:
                return _FallbackLLM(claude, gemini), None, name
            notice = CLAUDE_NOT_CONFIGURED
            return (gemini if gemini is not None and gemini.enabled else None), notice, name
        return (gemini if gemini is not None and gemini.enabled else None), None, name


def notice_of(client, initial: str | None) -> str | None:
    """Aviso final de la petición (inicial o producido por un cambio en tiempo de ejecución)."""
    return getattr(client, "notice", None) or initial if client is not None else initial
