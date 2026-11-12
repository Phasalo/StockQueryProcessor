from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from http.client import HTTPConnection, HTTPException, HTTPSConnection
from math import isfinite
from urllib.parse import urlsplit

from semantic_entry.core.commands.extraction import ExtractionContractError


@dataclass(frozen=True)
class YandexLlmConfig:
    model_uri: str
    api_key: str = field(repr=False)
    timeout_seconds: float = 30.0
    max_tokens: int = 2048
    endpoint: str = "https://ai.api.cloud.yandex.net/foundationModels/v1/completion"

    def __post_init__(self) -> None:
        if not isinstance(self.model_uri, str) or re.fullmatch(r"gpt://[^/\s]+/[^\s?#]+", self.model_uri) is None:
            raise ValueError("Требуется полный URI модели Yandex AI Studio.")
        if not isinstance(self.api_key, str) or not self.api_key or any(char.isspace() for char in self.api_key):
            raise ValueError("Требуется API-ключ без пробелов и переводов строк.")
        if type(self.max_tokens) is not int or not 0 < self.max_tokens < 2**63:
            raise ValueError("Лимит токенов должен быть положительным int64.")
        self._validate_connection()

    def _validate_connection(self) -> None:
        if isinstance(self.timeout_seconds, bool) or not isinstance(self.timeout_seconds, (int, float)):
            raise ValueError("Таймаут должен быть положительным конечным числом.")
        if not isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("Таймаут должен быть положительным конечным числом.")
        if not isinstance(self.endpoint, str):
            raise ValueError("Адрес API должен быть строкой.")
        endpoint = urlsplit(self.endpoint)
        if endpoint.scheme not in ("https", "http") or not endpoint.hostname:
            raise ValueError("Требуется абсолютный HTTP(S)-адрес API.")
        if endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
            raise ValueError("Адрес API не должен содержать авторизацию, query или fragment.")
        _ = endpoint.port


class YandexApiError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class YandexCompletionClient:
    def __init__(self, config: YandexLlmConfig):
        self._config = config

    def complete(self, system_prompt: str, user_text: str) -> str:
        body = json.dumps({
            "modelUri": self._config.model_uri,
            "completionOptions": {
                "stream": False, "temperature": 0,
                "maxTokens": str(self._config.max_tokens),
                "reasoningOptions": {"mode": "DISABLED"},
            },
            "jsonObject": True,
            "messages": [
                {"role": "system", "text": system_prompt},
                {"role": "user", "text": user_text},
            ],
        }, ensure_ascii=False, allow_nan=False).encode("utf-8")
        endpoint = urlsplit(self._config.endpoint)
        connection_type = HTTPSConnection if endpoint.scheme == "https" else HTTPConnection
        connection = connection_type(endpoint.hostname, endpoint.port, timeout=self._config.timeout_seconds)
        try:
            connection.request("POST", endpoint.path or "/", body=body, headers={
                "Authorization": f"Api-Key {self._config.api_key}",
                "Content-Type": "application/json",
                "x-data-logging-enabled": "false",
            })
            response = connection.getresponse()
            if response.status != 200:
                raise YandexApiError("Yandex AI Studio вернул ошибку HTTP.", status_code=response.status)
            return self._completion_text(response.read())
        except (OSError, HTTPException) as error:
            raise YandexApiError("Не удалось выполнить запрос к Yandex AI Studio.") from error
        finally:
            connection.close()

    @staticmethod
    def _completion_text(response: bytes) -> str:
        try:
            payload = json.loads(response)
            result = payload["result"] if "result" in payload else payload
            alternatives = result["alternatives"]
            if not isinstance(alternatives, list) or len(alternatives) != 1:
                raise ValueError("Expected one completion alternative")
            alternative = alternatives[0]
            if alternative["status"] != "ALTERNATIVE_STATUS_FINAL":
                raise ValueError("Completion is not final")
            message = alternative["message"]
            if message["role"] != "assistant":
                raise ValueError("Unexpected completion role")
            text = message["text"]
            if not isinstance(text, str) or not text.strip():
                raise ValueError("Empty completion text")
            return text
        except (KeyError, TypeError, ValueError) as error:
            raise ExtractionContractError("Yandex AI Studio вернул некорректный или незавершённый ответ.") from error
