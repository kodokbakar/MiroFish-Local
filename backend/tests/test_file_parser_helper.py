"""Unit tests for item 024 changes: FileParser.is_supported, Config.validate, and logger helpers."""

import inspect
import typing
from unittest.mock import patch

import pytest

from app.config import Config
from app.utils.file_parser import FileParser
from app.utils import logger as logger_module


class TestFileParserIsSupported:
    """Test suite for FileParser.is_supported() format checks."""

    @pytest.mark.parametrize(
        "file_path",
        [
            "doc.pdf",
            "doc.PDF",
            "doc.Pdf",
            "/tmp/path/guide.md",
            "README.MD",
            "notes.Markdown",
            "report.markdown",
            "data.MARKDOWN",
            "sample.txt",
            "LOG.TXT",
            "C:\\path\\windows\\test.Txt",
        ],
    )
    def test_supported_extensions(self, file_path: str):
        assert FileParser.is_supported(file_path) is True

    @pytest.mark.parametrize(
        "file_path",
        [
            "document.doc",
            "document.DOC",
            "binary.exe",
            "photo.jpg",
            "image.png",
            "",
            "no_extension",
            ".",
            "/path/to/folder/",
        ],
    )
    def test_unsupported_extensions(self, file_path: str):
        assert FileParser.is_supported(file_path) is False

    def test_supported_extensions_set(self):
        assert FileParser.SUPPORTED_EXTENSIONS == {".pdf", ".md", ".markdown", ".txt"}


class TestConfigValidate:
    """Test suite for Config.validate() return type hint and validation behavior."""

    def test_validate_return_type_hint(self):
        type_hints = typing.get_type_hints(Config.validate)
        assert type_hints.get("return") == list[str]

    def test_validate_returns_empty_list_when_valid(self):
        with patch.object(Config, "LLM_API_KEY", "ollama"), \
             patch.object(Config, "NEO4J_URI", "bolt://localhost:7687"), \
             patch.object(Config, "NEO4J_PASSWORD", "password"), \
             patch.object(Config, "EMBEDDING_PROVIDER", "ollama"), \
             patch.object(Config, "DEBUG", False):
            errors = Config.validate()
            assert isinstance(errors, list)
            assert errors == []

    def test_validate_missing_required_keys(self):
        with patch.object(Config, "LLM_API_KEY", ""), \
             patch.object(Config, "NEO4J_URI", ""), \
             patch.object(Config, "NEO4J_PASSWORD", ""), \
             patch.object(Config, "EMBEDDING_PROVIDER", "ollama"), \
             patch.object(Config, "DEBUG", False):
            errors = Config.validate()
            assert isinstance(errors, list)
            assert any("LLM_API_KEY" in err for err in errors)
            assert any("NEO4J_URI" in err for err in errors)
            assert any("NEO4J_PASSWORD" in err for err in errors)

    def test_validate_gemini_missing_key(self):
        with patch.object(Config, "LLM_API_KEY", "key"), \
             patch.object(Config, "NEO4J_URI", "bolt://localhost:7687"), \
             patch.object(Config, "NEO4J_PASSWORD", "password"), \
             patch.object(Config, "EMBEDDING_PROVIDER", "gemini"), \
             patch.object(Config, "GEMINI_API_KEY", ""), \
             patch.object(Config, "DEBUG", False):
            errors = Config.validate()
            assert any("GEMINI_API_KEY" in err for err in errors)

    def test_validate_openai_missing_key(self):
        with patch.object(Config, "LLM_API_KEY", ""), \
             patch.object(Config, "NEO4J_URI", "bolt://localhost:7687"), \
             patch.object(Config, "NEO4J_PASSWORD", "password"), \
             patch.object(Config, "EMBEDDING_PROVIDER", "openai"), \
             patch.object(Config, "EMBEDDING_API_KEY", ""), \
             patch.object(Config, "DEBUG", False):
            errors = Config.validate()
            assert any("EMBEDDING_API_KEY" in err for err in errors)


class TestLoggerHelpers:
    """Test suite for convenience logger functions and annotations."""

    @pytest.mark.parametrize("level", ["debug", "info", "warning", "error", "critical"])
    def test_logger_function_annotations_and_signatures(self, level: str):
        fn = getattr(logger_module, level)
        hints = typing.get_type_hints(fn)
        assert hints.get("msg") is str
        assert hints.get("return") in (type(None), None)

        sig = inspect.signature(fn)
        params = list(sig.parameters.values())
        assert len(params) >= 3
        assert params[0].name == "msg"
        assert params[1].kind == inspect.Parameter.VAR_POSITIONAL
        assert params[2].kind == inspect.Parameter.VAR_KEYWORD

    @pytest.mark.parametrize("level", ["debug", "info", "warning", "error", "critical"])
    def test_logger_function_calls_underlying_logger(self, level: str):
        fn = getattr(logger_module, level)
        with patch.object(logger_module.logger, level) as mock_log:
            result = fn("test message: %s", "arg1", extra={"key": "val"})
            assert result is None
            mock_log.assert_called_once_with("test message: %s", "arg1", extra={"key": "val"})
