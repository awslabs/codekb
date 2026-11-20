import { useState, useCallback } from "react";
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
  label = "Copy",
  disabled = false,
}: CopyButtonProps) {
  const [copied, setCopied] = useState(false);

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
        ariaLabel={copied ? "Copied" : label}
      >
        {variant === "icon" ? undefined : copied ? "Copied" : label}
      </Button>
    </span>
  );
}
