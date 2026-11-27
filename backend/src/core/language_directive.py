"""Language resolution for the downstream LLM prompt gateways.

This module owns the single "language code -> prompt display name" mapping and
the allow-list check derived from its keys. It turns a set-once space language
setting (read via ``CodeKBService.read_space_language_settings``) into a
``LanguageDirective`` value object that the Overview and analysis prompt
gateways can inject.

Design notes:
- ``en`` is the default. Both an unset (``None``) value and any value outside
  the mapping resolve to the default, and the default requires no instruction
  line, so a space without a language setting produces byte-for-byte identical
  prompts.
- Resolution never mutates the stored setting; it is a pure, in-scope transform.
- A value that falls outside the mapping is never echoed into the instruction
  line -- only the mapped display name is ever emitted.
- Overview and analysis are resolved independently; neither reads the other's
  result, and the UI display language is never an input.
"""

from __future__ import annotations

from dataclasses import dataclass

from .logging_utils import get_logger

logger = get_logger(__name__)

# Default language code. An unset or unsupported setting resolves here, and the
# default never adds an instruction line, keeping existing behaviour unchanged.
DEFAULT_LANGUAGE_CODE = "en"

# The single "language code -> prompt display name" mapping. Its key set is the
# allow-list used for resolution. Adding a language means adding one row here
# (plus the matching frontend definition). The display name is the English word
# embedded into the English prompt, distinct from the UI-facing display name
# (which uses each language's own native-script label).
_LANGUAGE_DISPLAY_NAMES: dict[str, str] = {
    "en": "English",
    "ja": "Japanese",
}


@dataclass(frozen=True)
class LanguageDirective:
    """Resolved, injectable form of a space language setting (value object).

    Not persisted; produced fresh on each resolution. ``instruction_required``
    is ``False`` exactly when the resolved code is the default, which is the
    invariant that keeps default/unset spaces on their pre-existing prompts.
    """

    resolved_language_code: str
    prompt_language_name: str
    instruction_required: bool

    def instruction_line(self) -> str:
        """Return the one-line instruction to append to an English prompt.

        Empty string when no instruction is required, so callers can append
        unconditionally without changing a default-language prompt.
        """
        if not self.instruction_required:
            return ""
        return f"Write the output in {self.prompt_language_name}."


def resolve_language_directive(
    language_code: str | None,
    *,
    space_id: str | None = None,
    purpose: str | None = None,
) -> LanguageDirective:
    """Resolve a raw language setting into a ``LanguageDirective``.

    Args:
        language_code: The raw setting read from the space status
            (``overview_language`` or ``analysis_language``). ``None`` means
            unset.
        space_id: The space the setting belongs to; used only for the
            round-down warning log. Optional.
        purpose: A short label for the log ("overview" / "analysis"); used only
            for the round-down warning log. Optional.

    Returns:
        A ``LanguageDirective``. Unset (``None``) and any value outside the
        allow-list both resolve to the default (``en``) with
        ``instruction_required=False``; the raw value never reaches the
        directive.
    """
    if language_code in _LANGUAGE_DISPLAY_NAMES:
        return LanguageDirective(
            resolved_language_code=language_code,
            prompt_language_name=_LANGUAGE_DISPLAY_NAMES[language_code],
            instruction_required=language_code != DEFAULT_LANGUAGE_CODE,
        )

    # Only warn on a genuine round-down: an explicitly set value that is not in
    # the allow-list. An unset (None) setting is the normal default path and
    # must stay silent.
    if language_code is not None:
        logger.warning(
            "Language setting rounded down to default: "
            "space_id=%s, purpose=%s, received=%r, resolved=%s",
            space_id,
            purpose,
            language_code,
            DEFAULT_LANGUAGE_CODE,
        )

    return LanguageDirective(
        resolved_language_code=DEFAULT_LANGUAGE_CODE,
        prompt_language_name=_LANGUAGE_DISPLAY_NAMES[DEFAULT_LANGUAGE_CODE],
        instruction_required=False,
    )
