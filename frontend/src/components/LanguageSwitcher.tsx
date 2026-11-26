/**
 * LanguageSwitcher — the header control for switching the UI display language.
 *
 * Cloudscape's `TopNavigation` takes descriptor objects in `utilities` rather
 * than arbitrary React children, so this module exposes a builder that produces
 * the `menu-dropdown` descriptor to place in `TopNavigation.utilities`.
 *
 * `i18n.language` is the single source of truth for the current language; no
 * local copy of that state is kept. The options are derived from the supported
 * language definition (`SUPPORTED_LANGUAGES`).
 */

import type { TopNavigationProps } from "@cloudscape-design/components/top-navigation";
import type { i18n as I18nInstance, TFunction } from "i18next";

import { persistLanguage } from "../i18n";
import {
  SUPPORTED_LANGUAGES,
  isSupportedLanguage,
  type LanguageCode,
} from "../i18n/languages";

/**
 * Switch the UI display language.
 * Applies immediately via `i18n.changeLanguage`, then persists to localStorage
 * on a best-effort basis.
 */
export function changeLanguage(i18n: I18nInstance, code: LanguageCode): void {
  void i18n.changeLanguage(code); // Apply immediately (re-renders the whole UI).
  persistLanguage(code); // Persist to browser-local storage (best-effort).
}

/**
 * Build the `menu-dropdown` descriptor for the language switcher to place in
 * `TopNavigation.utilities`.
 *
 * @param t Translation function (the label resolves via the `nav.language` key).
 * @param i18n The i18n instance used to read and switch the current language.
 */
export function buildLanguageSwitcherUtility(
  t: TFunction,
  i18n: I18nInstance
): TopNavigationProps.MenuDropdownUtility {
  const currentLanguage = i18n.language;

  return {
    type: "menu-dropdown",
    text: t("nav.language"),
    iconName: "settings",
    ariaLabel: t("nav.language"),
    items: SUPPORTED_LANGUAGES.map((lang) => ({
      id: lang.languageCode,
      text: lang.displayName,
      // Show a check mark on the currently selected language.
      itemType: "checkbox" as const,
      checked: lang.languageCode === currentLanguage,
    })),
    onItemClick: (event) => {
      const selected = event.detail.id;
      // Validation boundary: check the descriptor-supplied id against the
      // allow-list before acting on it.
      if (isSupportedLanguage(selected)) {
        changeLanguage(i18n, selected);
      }
    },
  };
}
