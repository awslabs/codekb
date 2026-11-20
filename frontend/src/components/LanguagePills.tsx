// Shared language "pill" badges with per-language brand colors.
// Used on the Overview metrics and the Create space form so language
// chips look consistent across the app.

const LANG_COLORS: Record<string, string> = {
  python: "#2b7b3a",
  java: "#b07219",
  typescript: "#3178c6",
  javascript: "#b08800",
  kotlin: "#A97BFF",
  go: "#00ADD8",
  rust: "#dea584",
  cpp: "#f34b7d",
  c: "#555555",
  csharp: "#178600",
  php: "#777bb4",
  ruby: "#701516",
  terraform: "#7B42BC",
  dart: "#0175C2",
};

// Normalize a display name or identifier to a LANG_COLORS key.
function normalizeLang(lang: string): string {
  const key = lang.toLowerCase().trim();
  if (key === "c#" || key === "c-sharp" || key === "c sharp") return "csharp";
  if (key === "c++") return "cpp";
  if (key === "js") return "javascript";
  if (key === "ts") return "typescript";
  if (key === "rb") return "ruby";
  return key;
}

export function languageColor(lang: string): string {
  return LANG_COLORS[normalizeLang(lang)] || "#687078";
}

// The languages CodeKB can analyze, as display names. This is the single
// frontend source of truth — the Create space form and the Documentation page
// both render from it, so they cannot drift apart.
//
// Mirrors `analysis.supported_languages` in the backend's core/default.yaml.
// When adding a language, add a LANG_COLORS entry above to match (normalizeLang
// handles display names that differ from the config key, such as "C#").
export const SUPPORTED_LANGUAGES = [
  "Python",
  "JavaScript",
  "TypeScript",
  "Java",
  "Kotlin",
  "Go",
  "C#",
  "Rust",
  "PHP",
  "Ruby",
  "C",
  "C++",
  "Terraform",
  "Dart",
];

export default function LanguagePills({
  languages,
  marginTop = 0,
}: {
  languages: string[];
  marginTop?: number;
}) {
  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: 4, marginTop }}>
      {languages.map((lang) => {
        const color = languageColor(lang);
        return (
          <span
            key={lang}
            style={{
              display: "inline-block",
              padding: "2px 10px",
              borderRadius: 12,
              fontSize: 12,
              fontWeight: 600,
              backgroundColor: `${color}18`,
              color,
              border: `1px solid ${color}40`,
            }}
          >
            {lang}
          </span>
        );
      })}
    </div>
  );
}
