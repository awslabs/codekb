import { useState, useCallback } from "react";
import { useTranslation } from "react-i18next";
import Button from "@cloudscape-design/components/button";

interface CopyButtonProps {
  text: string;
  variant?: "icon" | "inline-icon" | "normal";
  label?: string;
  disabled?: boolean;
}

export default function CopyButton({
  text,
  variant = "icon",
  label,
  disabled = false,
}: CopyButtonProps) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);
  const buttonLabel = label ?? t("common.copy");

  const handleCopy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // Fallback for older browsers
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    }
  }, [text]);

  return (
    <span onClick={(e) => e.stopPropagation()} onMouseDown={(e) => e.stopPropagation()}>
      <Button
        variant={variant}
        iconName={copied ? "status-positive" : "copy"}
        onClick={handleCopy}
        disabled={disabled}
        ariaLabel={copied ? t("common.copied") : buttonLabel}
      >
        {variant === "icon" ? undefined : copied ? t("common.copied") : buttonLabel}
      </Button>
    </span>
  );
}
