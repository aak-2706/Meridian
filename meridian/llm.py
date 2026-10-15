from __future__ import annotations

import json
import base64

import httpx

from .config import settings


class LLMClient:
    """Small OpenAI-compatible adapter; callers own role-specific prompts and validation."""
    def __init__(self):
        self.base_url = settings.llm_base_url
        self.api_key = settings.llm_api_key
        self.model = settings.llm_model

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)

    def _provider_error(self, response: httpx.Response) -> str:
        try:
            payload = response.json()
            error = payload.get("error", payload) if isinstance(payload, dict) else payload
            message = error.get("message") or error.get("code") if isinstance(error, dict) else str(error)
        except (ValueError, AttributeError):
            message = response.text
        message = str(message or "").replace(self.api_key, "[redacted]")
        return " ".join(message.split())[:500]

    def complete_json(self, system_prompt: str, payload: dict,
                      timeout: float = 20.0) -> tuple[dict, int | None]:
        if not self.configured:
            raise RuntimeError("LLM provider is not configured")
        body = {"model": self.model, "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system_prompt},
                         {"role": "user", "content": json.dumps(payload)}]}
        try:
            response = httpx.post(self.base_url + "/chat/completions", json=body,
                headers={"Authorization": f"Bearer {self.api_key}"}, timeout=timeout)
            response.raise_for_status()
            result = response.json()
        except httpx.HTTPStatusError as exc:
            message = self._provider_error(exc.response)
            suffix = f": {message}" if message else ""
            raise RuntimeError(f"Configured LLM request rejected (HTTP {exc.response.status_code}){suffix}") from exc
        except (httpx.RequestError, ValueError) as exc:
            raise RuntimeError(f"Configured LLM request failed: {exc}") from exc
        try:
            parsed = json.loads(result["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("LLM response did not contain valid JSON") from exc
        usage = result.get("usage", {}).get("total_tokens")
        return parsed, int(usage) if usage is not None else None

    def describe_document_page(self, image_bytes: bytes) -> str:
        if not self.configured or not settings.vision_enabled:
            raise RuntimeError("Vision processing is not enabled for the configured LLM")
        image_url = "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii")
        body = {"model": self.model, "temperature": 0,
            "messages": [{"role": "system", "content": "Transcribe the visible financial disclosure page as faithfully as possible. Preserve numbers, signs, units, row and column labels, footnotes, and headings. Do not summarize, infer missing text, or add commentary. Return only transcription."},
                {"role": "user", "content": [{"type": "text", "text": "Transcribe this document page."},
                    {"type": "image_url", "image_url": {"url": image_url}}]}]}
        try:
            response = httpx.post(self.base_url + "/chat/completions", json=body,
                headers={"Authorization": f"Bearer {self.api_key}"}, timeout=120)
            response.raise_for_status()
            result = response.json()
            text = result["choices"][0]["message"]["content"]
        except httpx.HTTPStatusError as exc:
            detail = self._provider_error(exc.response)
            suffix = f": {detail}" if detail else ""
            raise RuntimeError(f"Vision provider request rejected (HTTP {exc.response.status_code}){suffix}") from exc
        except (httpx.RequestError, ValueError, KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"Vision provider request failed: {exc}") from exc
        if isinstance(text, list):
            text = "\n".join(part.get("text", "") for part in text if isinstance(part, dict))
        return str(text).strip()
