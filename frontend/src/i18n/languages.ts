/**
 * Supported UI languages — the single source of truth.
 *
 * This module holds the set of languages the UI can display. The space
 * output-language form imports it to build its language options (a plain
 * module reference within the frontend, not a network contract).
 *
 * Adding a language means adding one row to the array below plus the matching
 * `src/locales/<code>.json` resource file.
 */

/** Supported language code (the allow-list element type). */
export type LanguageCode = "en" | "ja";

/** A supported-language entry used by the language selection UI. */
export interface SupportedLanguage {
  /** Language subtag (lowercase ASCII, no region subtag). */
  languageCode: LanguageCode;
  /** Language name shown in the switcher, written in that language. */
  displayName: string;
}

/**
 * The supported languages. The first entry is the default and fallback
 * language (`en`).
 */
export const SUPPORTED_LANGUAGES: readonly SupportedLanguage[] = [
  { languageCode: "en", displayName: "English" },
  { languageCode: "ja", displayName: "日本語" },
] as const;

/**
 * Allow-list of supported language codes. Acts as the validation boundary for
 * the persisted preference and any other input. It is a static array; language
 * codes are never used to build file paths.
 */
export const SUPPORTED_LANGUAGE_CODES: readonly LanguageCode[] =
  SUPPORTED_LANGUAGES.map((lang) => lang.languageCode);

/** The default and fallback language. */
export const DEFAULT_LANGUAGE: LanguageCode = "en";

/**
 * Type guard telling whether a value is one of the supported language codes.
 * Unknown or malformed values (path-like strings, for example) are rejected
 * here and callers fall back to `en`.
 */
export function isSupportedLanguage(value: unknown): value is LanguageCode {
  return (
    typeof value === "string" &&
    (SUPPORTED_LANGUAGE_CODES as readonly string[]).includes(value)
  );
}
