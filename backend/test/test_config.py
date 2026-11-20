"""Tests for ConfigurationManager precedence (default.yaml + env vars).

All configuration flows through default.yaml (base) plus CODEKB_* environment
variables. local.yaml support was removed — configuration is unified on env
vars across all deployments (ECS, customer .env, local dev).
"""

from pathlib import Path

from src.core.config import ConfigurationManager


class TestEnvOverrides:
    def test_string_override_sets_nested_path(self, monkeypatch):
        monkeypatch.setenv("CODEKB_LOG_LEVEL", "DEBUG")
        mgr = ConfigurationManager()
        mgr.load_configuration()
        assert mgr.get_config_value("logging.level") == "DEBUG"

    def test_int_coercion_for_numeric_env_vars(self, monkeypatch):
        monkeypatch.setenv("CODEKB_BEDROCK_MAX_TOKENS", "4096")
        mgr = ConfigurationManager()
        mgr.load_configuration()
        assert mgr.get_config_value("llm.defaults.max_tokens") == 4096

    def test_empty_llm_workload_var_is_treated_as_unset(self, monkeypatch):
        monkeypatch.setenv("CODEKB_SUMMARIZATION_MODEL_ID", "")
        mgr = ConfigurationManager()
        mgr.load_configuration()
        assert mgr.get_config_value("llm.workloads.summarization.model_id") is None

    def test_empty_value_still_sets_other_string_vars(self, monkeypatch):
        # Only the per-workload LLM vars treat "" as unset; an empty shared
        # model id must still fail loud downstream rather than fall back.
        monkeypatch.setenv("CODEKB_BEDROCK_MODEL_ID", "")
        mgr = ConfigurationManager()
        mgr.load_configuration()
        assert mgr.get_config_value("llm.defaults.model_id") == ""

    def test_invalid_int_is_skipped_not_raised(self, monkeypatch):
        monkeypatch.setenv("CODEKB_BEDROCK_MAX_TOKENS", "not-a-number")
        mgr = ConfigurationManager()
        mgr.load_configuration()
        value = mgr.get_config_value("llm.defaults.max_tokens")
        assert value != "not-a-number"

    def test_missing_env_var_leaves_default(self, monkeypatch):
        monkeypatch.delenv("CODEKB_LOG_LEVEL", raising=False)
        mgr = ConfigurationManager()
        mgr.load_configuration()
        level = mgr.get_config_value("logging.level")
        assert level
        assert isinstance(level, str)


class TestLocalOverridesRemoved:
    """local.yaml support was removed. Verify the method is a no-op."""

    def test_local_yaml_is_ignored(self, tmp_path, monkeypatch):
        """Even if ~/.codekb/local.yaml exists, it has no effect."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        local_dir = fake_home / ".codekb"
        local_dir.mkdir()
        (local_dir / "local.yaml").write_text(
            "storage:\n  s3:\n    bucket_name: should-be-ignored\n"
        )
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

        mgr = ConfigurationManager()
        mgr.load_configuration()
        # The override should NOT be applied
        assert mgr.get_config_value("storage.s3.bucket_name") != "should-be-ignored"


class TestExcludePatternsExtra:
    """CODEKB_EXCLUDE_PATTERNS_EXTRA EXTENDS analysis.exclude_patterns."""

    def test_extra_patterns_are_appended_to_defaults(self, monkeypatch):
        monkeypatch.setenv("CODEKB_EXCLUDE_PATTERNS_EXTRA", "vendor,coverage")
        mgr = ConfigurationManager()
        mgr.load_configuration()
        patterns = mgr.get_exclude_patterns()
        # Built-ins are retained (directory patterns keep their trailing slash)...
        assert ".git/" in patterns
        assert "node_modules/" in patterns
        # ...and the extras are added.
        assert "vendor" in patterns
        assert "coverage" in patterns

    def test_builtins_are_never_dropped(self, monkeypatch):
        mgr = ConfigurationManager()
        mgr.load_configuration()
        baseline = set(mgr.get_exclude_patterns())

        monkeypatch.setenv("CODEKB_EXCLUDE_PATTERNS_EXTRA", "extra1,extra2")
        mgr2 = ConfigurationManager()
        mgr2.load_configuration()
        assert baseline.issubset(set(mgr2.get_exclude_patterns()))

    def test_commas_and_newlines_both_split(self, monkeypatch):
        monkeypatch.setenv("CODEKB_EXCLUDE_PATTERNS_EXTRA", "a,\n b ,c\n")
        mgr = ConfigurationManager()
        mgr.load_configuration()
        patterns = mgr.get_exclude_patterns()
        # Entries are stripped and empties dropped.
        assert {"a", "b", "c"}.issubset(set(patterns))
        assert "" not in patterns
        assert " b " not in patterns

    def test_duplicate_of_builtin_is_not_added_twice(self, monkeypatch):
        monkeypatch.setenv("CODEKB_EXCLUDE_PATTERNS_EXTRA", "node_modules")
        mgr = ConfigurationManager()
        mgr.load_configuration()
        assert mgr.get_exclude_patterns().count("node_modules") == 1

    def test_result_is_a_list(self, monkeypatch):
        # A raw string here would make downstream `for p in patterns` iterate
        # characters — guard against regressing to scalar replacement.
        monkeypatch.setenv("CODEKB_EXCLUDE_PATTERNS_EXTRA", "*.min.js")
        mgr = ConfigurationManager()
        mgr.load_configuration()
        assert isinstance(mgr.get_exclude_patterns(), list)

    def test_unset_leaves_defaults_unchanged(self, monkeypatch):
        monkeypatch.delenv("CODEKB_EXCLUDE_PATTERNS_EXTRA", raising=False)
        mgr = ConfigurationManager()
        mgr.load_configuration()
        patterns = mgr.get_exclude_patterns()
        assert isinstance(patterns, list) and patterns
        assert ".git/" in patterns


class TestValidation:
    def test_validate_configuration_returns_list(self):
        mgr = ConfigurationManager()
        mgr.load_configuration()
        errors = mgr.validate_configuration()
        assert isinstance(errors, list)

    def test_default_config_validates_clean(self):
        """The bundled default.yaml must pass validation with no errors."""
        mgr = ConfigurationManager()
        mgr.load_configuration()
        assert mgr.validate_configuration() == []


class TestLanguageRegistryExclusivity:
    """A language must be listed as supported or unsupported, never both."""

    def test_shipped_config_lists_every_language_once(self):
        mgr = ConfigurationManager()
        config = mgr.load_configuration()
        analysis = config["analysis"]
        overlap = set(analysis["supported_languages"]) & set(
            analysis.get("unsupported_languages", {})
        )
        assert overlap == set()
        # The entry this unit adds is one of the ones that must not overlap.
        assert "dart" in analysis["supported_languages"]
        assert "dart" not in analysis.get("unsupported_languages", {})

    def test_a_language_in_both_lists_fails_the_load(self, tmp_path):
        import pytest

        config_path = tmp_path / "ambiguous.yaml"
        config_path.write_text(
            "logging:\n"
            "  level: INFO\n"
            "storage:\n"
            "  backend: s3_vectors\n"
            "analysis:\n"
            "  supported_languages:\n"
            "    dart:\n"
            "      extensions: ['.dart']\n"
            "  unsupported_languages:\n"
            "    dart:\n"
            "      extensions: ['.dart']\n"
        )

        mgr = ConfigurationManager()
        with pytest.raises(ValueError, match="dart appears in both"):
            mgr.load_configuration(str(config_path))


class TestGeneratedFilePatternValidation:
    """A generated-file pattern list must be a list of non-blank text."""

    _BASE = (
        "logging:\n"
        "  level: INFO\n"
        "storage:\n"
        "  backend: s3_vectors\n"
        "analysis:\n"
        "  supported_languages:\n"
        "    dart:\n"
        "      extensions: ['.dart']\n"
        "      parser: tree-sitter-dart\n"
    )

    def test_shipped_pattern_lists_are_well_formed(self):
        """Every list in the bundled default.yaml is valid, and dart ships four."""
        mgr = ConfigurationManager()
        config = mgr.load_configuration()
        supported = config["analysis"]["supported_languages"]

        for language, lang_config in supported.items():
            patterns = lang_config.get("generated_file_patterns")
            if patterns is None:
                continue
            assert isinstance(patterns, list), language
            for entry in patterns:
                assert isinstance(entry, str) and entry.strip(), language

        # Dart ships the only non-empty default list.
        assert supported["dart"]["generated_file_patterns"] == [
            "*.g.dart",
            "*.freezed.dart",
            "*.gr.dart",
            "*.mocks.dart",
        ]
        with_patterns = [
            language
            for language, lang_config in supported.items()
            if lang_config.get("generated_file_patterns")
        ]
        assert with_patterns == ["dart"]

    def test_a_non_list_value_fails_the_load_naming_the_language(self, tmp_path):
        import pytest

        config_path = tmp_path / "not-a-list.yaml"
        config_path.write_text(self._BASE + "      generated_file_patterns: '*.g.dart'\n")

        mgr = ConfigurationManager()
        with pytest.raises(ValueError, match="dart.generated_file_patterns must be a list"):
            mgr.load_configuration(str(config_path))

    def test_a_blank_entry_fails_the_load_naming_the_entry(self, tmp_path):
        import pytest

        config_path = tmp_path / "blank-entry.yaml"
        config_path.write_text(
            self._BASE + "      generated_file_patterns:\n        - '*.g.dart'\n        - '   '\n"
        )

        mgr = ConfigurationManager()
        with pytest.raises(ValueError, match="dart.generated_file_patterns holds an invalid entry"):
            mgr.load_configuration(str(config_path))

    def test_an_empty_list_loads_cleanly(self, tmp_path):
        """An empty list is valid and means no exclusion."""
        config_path = tmp_path / "empty-list.yaml"
        config_path.write_text(self._BASE + "      generated_file_patterns: []\n")

        mgr = ConfigurationManager()
        config = mgr.load_configuration(str(config_path))
        assert config["analysis"]["supported_languages"]["dart"]["generated_file_patterns"] == []

    def test_shapes_the_validator_tolerates_do_not_raise(self):
        """A section this validator has nothing to say about is left to its owner.

        A missing or non-dict ``analysis`` or ``supported_languages`` is another
        validator's business; this one passes over it rather than raising a
        second, less specific error for the same defect.
        """
        mgr = ConfigurationManager()

        for config in (
            {},
            {"analysis": "not-a-dict"},
            {"analysis": {"supported_languages": "not-a-dict"}},
            {"analysis": {"supported_languages": {"dart": "not-a-dict"}}},
        ):
            mgr._config = config
            mgr._validate_generated_file_patterns()


class TestRequireConfig:
    def test_returns_value_present_in_default_yaml(self):
        from src.core.config import require_config

        # read_timeout ships in default.yaml, so this must resolve.
        assert int(require_config("llm.providers.bedrock.read_timeout")) > 0

    def test_raises_for_missing_key(self):
        import pytest

        from src.core.config import ConfigError, require_config

        with pytest.raises(ConfigError, match="missing"):
            require_config("summarization.this_key_does_not_exist")


class TestAPathShapedGeneratedPatternIsRefused:
    """Matching is against the basename, so a pattern with `/` can never fire.

    That is the exact failure this validator exists to prevent: the list loads
    cleanly and the user believes files are excluded when they are not.
    """

    _BASE = "analysis:\n  supported_languages:\n    dart:\n      extensions: ['.dart']\n"

    def test_a_pattern_containing_a_separator_fails_the_load(self, tmp_path):
        import pytest

        config_path = tmp_path / "path-shaped.yaml"
        config_path.write_text(
            self._BASE + "      generated_file_patterns:\n        - 'lib/*.g.dart'\n"
        )

        mgr = ConfigurationManager()
        with pytest.raises(ValueError, match="lib/\\*.g.dart"):
            mgr.load_configuration(str(config_path))

    def test_a_basename_pattern_still_loads(self, tmp_path):
        config_path = tmp_path / "basename.yaml"
        config_path.write_text(
            self._BASE
            + "      generated_file_patterns:\n        - '*.g.dart'\n        - '*.freezed.dart'\n"
        )

        mgr = ConfigurationManager()
        mgr.load_configuration(str(config_path))

        assert mgr.get_config_value(
            "analysis.supported_languages.dart.generated_file_patterns"
        ) == ["*.g.dart", "*.freezed.dart"]
