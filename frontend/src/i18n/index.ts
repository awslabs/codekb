/**
 * i18n initialisation: sets up the translation resources, the initial language
 * and the fallback behaviour.
 *
 * - Translation resources are bundled at build time (`src/locales/*.json` are
 *   imported statically); file paths are never built from a language code.
 * - Choosing the initial language is a purely local, synchronous operation
 *   (one localStorage read plus an allow-list check), so nothing awaits before
 *   the first render.
 */

import i18next from "i18next";
import { initReactI18next } from "react-i18next";

import en from "../locales/en.json";
import ja from "../locales/ja.json";
import {
  DEFAULT_LANGUAGE,
  SUPPORTED_LANGUAGE_CODES,
  isSupportedLanguage,
  type LanguageCode,
} from "./languages";

/** Browser-scoped localStorage key. Only a language code string is stored. */
export const LANGUAGE_STORAGE_KEY = "UiLanguagePreference";

/** Whether this is a development build (warnings are suppressed in production). */
const isDev = import.meta.env.DEV;

/**
 * Determine the initial UI language synchronously.
 *
 * Reads the persisted value and accepts it when it matches the supported
 * language allow-list. A missing or unsupported value (tampered or path-like
 * strings, for example) falls back to `en`.
 */
export function resolveInitialLanguage(): LanguageCode {
  try {
    const stored = window.localStorage.getItem(LANGUAGE_STORAGE_KEY);
    if (isSupportedLanguage(stored)) {
      return stored;
    }
  } catch (error) {
    // localStorage unavailable (private mode, for example). Recoverable: keep
    // going with the default language.
    if (isDev) {
      console.warn(
        "[i18n] Failed to read language preference from localStorage; falling back to default language.",
        error
      );
    }
  }
  return DEFAULT_LANGUAGE;
}

/**
 * Persist the selected language to localStorage on a best-effort basis.
 *
 * Only an already allow-listed language code is written. A write failure is
 * not fatal: the current selection still applies, it just will not survive a
 * reload.
 */
export function persistLanguage(code: LanguageCode): void {
  try {
    window.localStorage.setItem(LANGUAGE_STORAGE_KEY, code);
  } catch (error) {
    if (isDev) {
      console.warn(
        "[i18n] Failed to persist language preference to localStorage; the selection will not survive a reload.",
        error
      );
    }
  }
}

void i18next.use(initReactI18next).init({
  // Resources are registered statically from the bundle only.
  resources: {
    en: { translation: en },
    ja: { translation: ja },
  },
  lng: resolveInitialLanguage(),
  fallbackLng: DEFAULT_LANGUAGE, // Missing keys fall back to English.
  supportedLngs: [...SUPPORTED_LANGUAGE_CODES],
  // Keys are nested JSON objects addressed with dot-separated paths. Namespace
  // separation stays disabled: a `.` is a nesting boundary, never a namespace
  // boundary.
  keySeparator: ".",
  nsSeparator: false,
  interpolation: {
    // React escapes values when rendering JSX, so i18next's own escaping is
    // disabled to avoid double escaping.
    escapeValue: false,
  },
  // Detect missing keys when a fallback occurs.
  saveMissing: true,
  missingKeyHandler: (lngs, _ns, key) => {
    // Development only. The message carries just the key name and language
    // codes, and is suppressed in production.
    if (isDev) {
      console.warn(`[i18n] Missing translation key "${key}" for language(s): ${lngs.join(", ")}`);
    }
  },
});

export default i18next;
