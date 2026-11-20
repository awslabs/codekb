"""
Parser factory for creating language-specific parsers.
"""

import logging
from typing import Dict, Type
from .base_parser import BaseLanguageParser
from .python_parser import PythonParser
from .typescript_parser import TypeScriptParser
from .javascript_parser import JavaScriptParser
from .java_parser import JavaParser
from .kotlin_parser import KotlinParser
from .go_parser import GoParser
from .csharp_parser import CSharpParser
from .rust_parser import RustParser
from .cpp_parser import CppParser
from .php_parser import PhpParser
from .ruby_parser import RubyParser
from .terraform_parser import TerraformParser
from .dart_parser import DartParser

logger = logging.getLogger(__name__)


class ParserFactory:
    """Factory for creating language-specific parsers."""

    _parsers: Dict[str, Type[BaseLanguageParser]] = {
        "python": PythonParser,
        "typescript": TypeScriptParser,
        "javascript": JavaScriptParser,
        "java": JavaParser,
        "kotlin": KotlinParser,
        "go": GoParser,
        "csharp": CSharpParser,
        "rust": RustParser,
        "cpp": CppParser,
        "c": CppParser,
        "php": PhpParser,
        "ruby": RubyParser,
        "terraform": TerraformParser,
        "dart": DartParser,
    }

    @classmethod
    def create_parser(cls, language: str) -> BaseLanguageParser:
        """Create a parser for the specified language."""
        if language not in cls._parsers:
            logger.error(f"Unsupported language requested: {language}")
            raise ValueError(f"Unsupported language: {language}")
        logger.debug(f"Creating parser for language: {language}")
        return cls._parsers[language]()

    @classmethod
    def register_parser(cls, language: str, parser_class: Type[BaseLanguageParser]) -> None:
        """Register a new parser for a language."""
        cls._parsers[language] = parser_class
        logger.info(f"Registered parser for language: {language}")

    @classmethod
    def get_supported_languages(cls) -> list[str]:
        """Get list of supported languages."""
        return list(cls._parsers.keys())

    @classmethod
    def is_language_supported(cls, language: str) -> bool:
        """Check if a language is supported."""
        return language in cls._parsers
