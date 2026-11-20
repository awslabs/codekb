"""Every configured language must have a loaded parser.

Nothing compared the languages ``analysis.supported_languages`` configures with
the parsers ``TreeSitterWrapper`` actually loaded, so three separate defects in
the vendored Dart grammar's build were silent: a missing grammar is caught by
design, and ``CodeAnalyzer`` narrows the scanner to the loaded languages, so the
affected files were reported as an unsupported-language skip. The suite shared
the blind spot — the Dart tests carry ``skipif(not DART_AVAILABLE)`` and went
quietly yellow. The first test here is deliberately unguarded by that flag.
"""

from __future__ import annotations

import logging

import pytest

from src.core import tree_sitter_wrapper as tsw


@pytest.fixture
def isolated_wrapper():
    """Yield a factory for a freshly-constructed wrapper, then restore the shared one.

    ``TreeSitterWrapper`` is a ``__new__``-based singleton whose ``_initialized``
    flag short-circuits ``__init__``, so a second construction returns the wired
    instance and no monkeypatch on the module takes effect. Both are restored.
    """
    saved_instance = tsw.TreeSitterWrapper._instance
    saved_initialized = tsw.TreeSitterWrapper._initialized
    tsw.TreeSitterWrapper._instance = None
    tsw.TreeSitterWrapper._initialized = False
    try:
        yield tsw.TreeSitterWrapper
    finally:
        tsw.TreeSitterWrapper._instance = saved_instance
        tsw.TreeSitterWrapper._initialized = saved_initialized


def test_every_configured_language_has_a_loaded_parser() -> None:
    """A failure naming ``dart`` means the vendored grammar did not reach this
    interpreter as a loadable extension: the macOS extension suffix carries no
    architecture tag, so one source tree cannot hold a correct artifact for two.
    ``uv sync --reinstall-package aws-codekb-backend`` rebuilds it.
    """
    wrapper = tsw.TreeSitterWrapper()

    configured = set(wrapper.supported_languages)
    loaded = set(wrapper.parsers)

    assert configured, "analysis.supported_languages is empty; configuration did not load"
    assert not configured - loaded, (
        f"configured languages with no loaded parser: {sorted(configured - loaded)}. "
        f"Files in these languages are reported as skipped, not failed, so this "
        f"cannot be detected downstream. Loaded: {sorted(loaded)}"
    )


def test_a_configured_language_with_no_parser_is_warned_about(
    isolated_wrapper, caplog, monkeypatch
) -> None:
    """Each language's initialisation block is skipped by its own availability flag
    without comment, and the closing INFO line reports only what loaded. Driving
    ``DART_AVAILABLE`` false reproduces that without unimporting a loaded module.
    """
    monkeypatch.setattr(tsw, "DART_AVAILABLE", False, raising=False)

    with caplog.at_level(logging.WARNING, logger=tsw.__name__):
        wrapper = isolated_wrapper()

    assert "dart" not in wrapper.parsers
    assert "dart" in wrapper.supported_languages, "configuration must still list dart"

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("no loaded parser" in message and "dart" in message for message in warnings), (
        f"construction with no Dart parser emitted no warning naming it; got: {warnings}"
    )


def test_a_complete_parser_set_warns_about_nothing(isolated_wrapper, caplog) -> None:
    """The healthy case stays quiet, so the warning means something when it fires:
    a warning emitted unconditionally would be ignored within a week.
    """
    with caplog.at_level(logging.WARNING, logger=tsw.__name__):
        wrapper = isolated_wrapper()

    assert not set(wrapper.supported_languages) - set(wrapper.parsers)

    warnings = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and "no loaded parser" in r.getMessage()
    ]
    assert warnings == [], f"a complete parser set must warn about nothing; got: {warnings}"
