import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";

export type Locale = "zh" | "en";
export const LANGUAGE_KEY = "paper-alpha-language";
type I18n = { locale: Locale; setLocale: (locale: Locale) => void; t: (zh: string, en: string) => string };
const fallback: I18n = { locale: "zh", setLocale: () => {}, t: (zh) => zh };
const LanguageContext = createContext<I18n>(fallback);
let sessionLocale: Locale | undefined;
export function readLocale(): Locale {
  if (sessionLocale) return sessionLocale;
  try { return localStorage.getItem(LANGUAGE_KEY) === "en" ? "en" : "zh"; } catch { return "zh"; }
}
export function translate(zh: string, en: string): string { return readLocale() === "en" ? en : zh; }
export function LanguageProvider({ children }: { children: ReactNode }) {
  const [locale, updateLocale] = useState<Locale>(readLocale);
  const setLocale = useCallback((value: Locale) => {
    sessionLocale = value;
    try { localStorage.setItem(LANGUAGE_KEY, value); } catch { /* Keep the language for this session. */ }
    updateLocale(value);
  }, []);
  const t = useCallback((zh: string, en: string) => locale === "en" ? en : zh, [locale]);
  useEffect(() => {
    document.documentElement.lang = locale === "zh" ? "zh-CN" : "en";
    document.title = locale === "zh" ? "Paper-to-Alpha · 研究工作台" : "Paper-to-Alpha · Research workbench";
    try { localStorage.setItem(LANGUAGE_KEY, locale); } catch { /* The current session remains usable. */ }
  }, [locale]);
  return <LanguageContext.Provider value={{ locale, setLocale, t }}>{children}</LanguageContext.Provider>;
}
export function useI18n() { return useContext(LanguageContext); }
export function LanguageSwitch() {
  const { locale, setLocale, t } = useI18n();
  return <div className="language-switch" role="group" aria-label={t("界面语言", "Interface language")}>
    <button type="button" lang="zh-CN" aria-pressed={locale === "zh"} onClick={() => setLocale("zh")}>中文</button>
    <button type="button" lang="en" aria-pressed={locale === "en"} onClick={() => setLocale("en")}>English</button>
  </div>;
}
