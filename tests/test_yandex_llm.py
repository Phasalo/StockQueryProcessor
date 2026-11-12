import json
import unittest
from dataclasses import replace
from datetime import timedelta
from http.client import HTTPException
from unittest.mock import Mock, patch

from app.bootstrap import build_yandex_extractor
from semantic_entry.adapters.llm.yandex import (
    YandexApiError,
    YandexCompletionClient,
    YandexLlmConfig,
)
from semantic_entry.adapters.memory.drafts import MemoryDraftStore
from semantic_entry.core.access import Actor
from semantic_entry.core.commands.extraction import ExtractionContractError
from semantic_entry.modules.warehouse.bootstrap import build_warehouse_engine


def completion_response(text='{"status":"no_match"}', *, status="ALTERNATIVE_STATUS_FINAL", wrapped=True):
    result = {"alternatives": [{"message": {"role": "assistant", "text": text}, "status": status}], "usage": {"totalTokens": "10"}}
    return json.dumps({"result": result} if wrapped else result).encode("utf-8")


class YandexCompletionClientTest(unittest.TestCase):
    def setUp(self):
        self.config = YandexLlmConfig(model_uri="gpt://folder/model/latest", api_key="secret-test-key", timeout_seconds=7, max_tokens=500)
        self.connection = Mock()
        self.response = Mock(status=200)
        self.response.read.return_value = completion_response()
        self.connection.getresponse.return_value = self.response
        self.patcher = patch("semantic_entry.adapters.llm.yandex.HTTPSConnection", return_value=self.connection)
        self.connection_factory = self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.client = YandexCompletionClient(self.config)

    def test_constructor_does_not_open_connection(self):
        self.connection_factory.assert_not_called()
        self.connection.request.assert_not_called()

    def test_request_uses_authorization_json_mode_and_separate_messages(self):
        self.assertEqual(self.client.complete("System", "Приход"), '{"status":"no_match"}')
        self.connection_factory.assert_called_once_with("ai.api.cloud.yandex.net", None, timeout=7)
        args, kwargs = self.connection.request.call_args
        self.assertEqual(args, ("POST", "/foundationModels/v1/completion"))
        self.assertEqual(kwargs["headers"]["Authorization"], "Api-Key secret-test-key")
        self.assertEqual(kwargs["headers"]["x-data-logging-enabled"], "false")
        body = json.loads(kwargs["body"])
        self.assertEqual(body["modelUri"], self.config.model_uri)
        self.assertTrue(body["jsonObject"])
        self.assertEqual(body["completionOptions"], {"stream": False, "temperature": 0, "maxTokens": "500", "reasoningOptions": {"mode": "DISABLED"}})
        self.assertEqual(body["messages"], [{"role": "system", "text": "System"}, {"role": "user", "text": "Приход"}])
        self.connection.close.assert_called_once()

    def test_flat_completion_response_is_supported(self):
        self.response.read.return_value = completion_response(wrapped=False)
        self.assertEqual(self.client.complete("System", "Text"), '{"status":"no_match"}')

    def test_custom_endpoint_uses_http_transport_and_its_path(self):
        config = replace(self.config, endpoint="http://localhost:8765/completion")
        with patch("semantic_entry.adapters.llm.yandex.HTTPConnection", return_value=self.connection) as factory:
            YandexCompletionClient(config).complete("System", "Text")
        factory.assert_called_once_with("localhost", 8765, timeout=7)
        self.assertEqual(self.connection.request.call_args.args, ("POST", "/completion"))
        self.connection_factory.assert_not_called()

    def test_http_errors_and_redirects_are_not_followed_or_retried(self):
        for status in (301, 400, 401, 403, 429, 500):
            with self.subTest(status=status):
                self.connection.reset_mock()
                self.response.reset_mock()
                self.response.status = status
                with self.assertRaises(YandexApiError) as caught:
                    self.client.complete("System", "Private text")
                self.assertEqual(caught.exception.status_code, status)
                self.assertNotIn(self.config.api_key, str(caught.exception))
                self.assertNotIn("Private text", str(caught.exception))
                self.connection.request.assert_called_once()
                self.connection.close.assert_called_once()
                self.response.read.assert_not_called()

    def test_network_errors_close_connection_and_do_not_retry(self):
        for error in (TimeoutError("Timeout"), OSError("Network unavailable"), HTTPException("Broken response")):
            with self.subTest(error=error):
                self.connection.reset_mock()
                self.connection.request.side_effect = error
                with self.assertRaises(YandexApiError) as caught:
                    self.client.complete("System", "Text")
                self.assertIsNone(caught.exception.status_code)
                self.connection.request.assert_called_once()
                self.connection.close.assert_called_once()

    def test_incomplete_filtered_and_tool_responses_are_rejected(self):
        for status in ("ALTERNATIVE_STATUS_PARTIAL", "ALTERNATIVE_STATUS_TRUNCATED_FINAL", "ALTERNATIVE_STATUS_CONTENT_FILTER", "ALTERNATIVE_STATUS_TOOL_CALLS"):
            with self.subTest(status=status):
                self.connection.reset_mock()
                self.response.read.return_value = completion_response(status=status)
                with self.assertRaises(ExtractionContractError):
                    self.client.complete("System", "Text")
                self.connection.close.assert_called_once()

    def test_invalid_response_shapes_are_rejected(self):
        for payload in (None, [], {}, {"result": None}, {"alternatives": []}, {"alternatives": {}}, {"alternatives": [{}, {}]}, {"alternatives": [{"status": "ALTERNATIVE_STATUS_FINAL", "message": {"role": "user", "text": "Text"}}]}):
            with self.subTest(payload=payload):
                self.response.read.return_value = json.dumps(payload).encode()
                with self.assertRaises(ExtractionContractError):
                    self.client.complete("System", "Text")

    def test_malformed_json_and_empty_text_are_rejected(self):
        for response in (b"not JSON", completion_response(" "), completion_response(None)):
            with self.subTest(response=response):
                self.response.read.return_value = response
                with self.assertRaises(ExtractionContractError):
                    self.client.complete("System", "Text")

    def test_config_hides_secret_and_rejects_invalid_settings(self):
        self.assertNotIn(self.config.api_key, repr(self.config))
        for change in (
            {"model_uri": ""}, {"model_uri": "model"}, {"api_key": ""}, {"api_key": "key\nheader"},
            {"max_tokens": True}, {"max_tokens": 0}, {"max_tokens": 2**63},
            {"timeout_seconds": 0}, {"timeout_seconds": float("nan")}, {"timeout_seconds": float("inf")}, {"timeout_seconds": True},
            {"endpoint": "relative/path"}, {"endpoint": "https://user:secret@example.com/path"},
            {"endpoint": "https://example.com/path?key=secret"}, {"endpoint": "https://example.com/path#fragment"},
            {"endpoint": "https://example.com:invalid/path"}, {"endpoint": None},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                replace(self.config, **change)
        self.connection_factory.assert_not_called()

    def test_application_factory_is_lazy_and_composes_with_engine(self):
        extractor = build_yandex_extractor(self.config)
        policy = Mock()
        policy.is_allowed.return_value = True
        drafts = Mock(wraps=MemoryDraftStore())
        factory = Mock(side_effect=AssertionError("Unexpected database call"))
        engine = build_warehouse_engine(factory, policy, drafts=drafts, extractor=extractor)
        self.connection_factory.assert_not_called()
        result = engine.prepare_from_text("Unsupported request", Actor("alice"), lifetime=timedelta(minutes=5))
        self.assertIsNone(result.draft)
        self.connection.request.assert_called_once()
        factory.assert_not_called()
        drafts.create.assert_not_called()

    def test_truncated_response_does_not_prepare_draft(self):
        self.response.read.return_value = completion_response(status="ALTERNATIVE_STATUS_TRUNCATED_FINAL")
        policy = Mock()
        policy.is_allowed.return_value = True
        drafts = Mock(wraps=MemoryDraftStore())
        factory = Mock(side_effect=AssertionError("Unexpected database call"))
        engine = build_warehouse_engine(factory, policy, drafts=drafts, extractor=build_yandex_extractor(self.config))
        with self.assertRaises(ExtractionContractError):
            engine.prepare_from_text("Text", Actor("alice"), lifetime=timedelta(minutes=5))
        factory.assert_not_called()
        drafts.create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
