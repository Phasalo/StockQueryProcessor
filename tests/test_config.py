import unittest
from os import environ
from unittest.mock import patch

from app.bootstrap import build_yandex_extractor
from config import load_yandex_config


class YandexEnvironmentConfigTest(unittest.TestCase):
    def test_loader_uses_secret_key_and_model_uri(self):
        with patch.dict(environ, {
            "YANDEX_API_KEY": "test-secret", "YANDEX_API_KEY_ID": "key-id",
            "YANDEX_MODEL_URI": "gpt://folder/model/latest",
        }, clear=True):
            config = load_yandex_config()
        self.assertEqual(config.api_key, "test-secret")
        self.assertEqual(config.model_uri, "gpt://folder/model/latest")
        self.assertNotIn("test-secret", repr(config))

    def test_key_identifier_does_not_replace_secret_key(self):
        with patch.dict(environ, {
            "YANDEX_API_KEY_ID": "key-id", "YANDEX_MODEL_URI": "gpt://folder/model",
        }, clear=True):
            with self.assertRaisesRegex(ValueError, "YANDEX_API_KEY$"):
                load_yandex_config()

    def test_missing_model_uri_has_clear_error_without_secret(self):
        with patch.dict(environ, {"YANDEX_API_KEY": "test-secret"}, clear=True):
            with self.assertRaises(ValueError) as caught:
                load_yandex_config()
        self.assertIn("YANDEX_MODEL_URI", str(caught.exception))
        self.assertNotIn("test-secret", str(caught.exception))

    def test_settings_are_read_when_loading_instead_of_at_import(self):
        with patch.dict(environ, {
            "YANDEX_API_KEY": "first-secret", "YANDEX_MODEL_URI": "gpt://folder/model",
        }, clear=True):
            first = load_yandex_config()
            environ["YANDEX_API_KEY"] = "second-secret"
            second = load_yandex_config()
        self.assertEqual(first.api_key, "first-secret")
        self.assertEqual(second.api_key, "second-secret")

    def test_application_factory_reads_environment_without_connecting(self):
        with patch.dict(environ, {
            "YANDEX_API_KEY": "test-secret", "YANDEX_MODEL_URI": "gpt://folder/model",
        }, clear=True), patch("semantic_entry.adapters.llm.yandex.HTTPSConnection") as connection:
            build_yandex_extractor()
            connection.assert_not_called()


if __name__ == "__main__":
    unittest.main()
