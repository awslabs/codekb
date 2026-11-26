/**
 * I18nProvider — a thin wrapper that wraps the existing app tree with the
 * react-i18next provider.
 *
 * The i18n instance initialisation (initial language resolution, fallback,
 * missing-key warnings) completes as a side effect of `./index.ts`. This
 * component's only responsibility is to provide the already-initialised
 * instance to the React tree.
 */

import type { ReactNode } from "react";
import { I18nextProvider } from "react-i18next";

import i18n from "./index";

export interface I18nProviderProps {
  /** The existing app tree to wrap. */
  children: ReactNode;
}

export function I18nProvider({ children }: I18nProviderProps) {
  return <I18nextProvider i18n={i18n}>{children}</I18nextProvider>;
}

export default I18nProvider;
