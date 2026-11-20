import { useMemo } from "react";
import { common, createLowlight } from "lowlight";
import { toJsxRuntime } from "hast-util-to-jsx-runtime";
import { Fragment, jsx, jsxs } from "react/jsx-runtime";
import "highlight.js/styles/github-dark.css";

const lowlight = createLowlight(common);

/** Map a file path / extension to a highlight.js language id. */
const EXT_TO_LANG: Record<string, string> = {
  ts: "typescript",
  tsx: "typescript",
  js: "javascript",
  jsx: "javascript",
  mjs: "javascript",
  cjs: "javascript",
  py: "python",
  java: "java",
  kt: "kotlin",
  go: "go",
  rs: "rust",
  rb: "ruby",
  rake: "ruby",
  gemspec: "ruby",
  php: "php",
  cs: "csharp",
  cpp: "cpp",
  cc: "cpp",
  cxx: "cpp",
  c: "c",
  h: "cpp",
  hpp: "cpp",
  swift: "swift",
  scala: "scala",
  sh: "bash",
  bash: "bash",
  zsh: "bash",
  yml: "yaml",
  yaml: "yaml",
  json: "json",
  xml: "xml",
  html: "xml",
  css: "css",
  scss: "scss",
  sql: "sql",
  md: "markdown",
  toml: "ini",
  ini: "ini",
  dockerfile: "dockerfile",
};

/** Infer a highlight.js language id from a file path, or undefined if unknown. */
export function languageFromPath(filePath?: string): string | undefined {
  if (!filePath) return undefined;
  const base = filePath.split("/").pop()?.toLowerCase() ?? "";
  // Extensionless files that are still source code.
  if (base === "dockerfile") return "dockerfile";
  if (base === "gemfile" || base === "rakefile") return "ruby";
  const ext = base.includes(".") ? base.split(".").pop()! : "";
  return EXT_TO_LANG[ext];
}

interface CodeBlockProps {
  code: string;
  /** Explicit highlight.js language id; falls back to auto-detection. */
  language?: string;
  className?: string;
}

/**
 * Syntax-highlighted code block using highlight.js. When a language is given
 * and supported it is used directly; otherwise highlight.js auto-detects.
 */
export default function CodeBlock({ code, language, className }: CodeBlockProps) {
  const highlighted = useMemo(() => {
    try {
      const tree =
        language && lowlight.registered(language)
          ? lowlight.highlight(language, code)
          : lowlight.highlightAuto(code);
      return toJsxRuntime(tree, { Fragment, jsx, jsxs });
    } catch {
      // Highlighting failed — fall back to rendering the raw code as plain text.
      return null;
    }
  }, [code, language]);

  return (
    <pre className={`hljs code-block${className ? ` ${className}` : ""}`}>
      <code>{highlighted !== null ? highlighted : code}</code>
    </pre>
  );
}
