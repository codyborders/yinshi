import { useState } from "react";

interface ToolCallCardProps {
  toolName: string;
  input: unknown;
}

export default function ToolCallCard({ toolName, input }: ToolCallCardProps) {
  const [expanded, setExpanded] = useState(false);

  const inputStr =
    typeof input === "string" ? input : JSON.stringify(input, null, 2);

  return (
    <div className="animate-message-in">
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex w-full items-center gap-2 rounded-xl bg-gray-800/70 px-3 py-2 text-left min-h-touch active:bg-gray-700"
      >
        <svg
          className="h-4 w-4 shrink-0 text-blue-400"
          fill="none"
          viewBox="0 0 24 24"
          strokeWidth={2}
          stroke="currentColor"
        >
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            d="M11.42 15.17 17.25 21A2.652 2.652 0 0 0 21 17.25l-5.877-5.877M11.42 15.17l2.496-3.03c.317-.384.74-.626 1.208-.766M11.42 15.17l-4.655 5.653a2.548 2.548 0 1 1-3.586-3.586l6.837-5.63m5.108-.233c.55-.164 1.163-.188 1.743-.14a4.5 4.5 0 0 0 4.486-6.336l-3.276 3.277a3.004 3.004 0 0 1-2.25-2.25l3.276-3.276a4.5 4.5 0 0 0-6.336 4.486c.049.58.025 1.193-.14 1.743"
          />
        </svg>
        <span className="flex-1 truncate text-xs font-mono text-gray-400">
          {toolName}
        </span>
        <svg
          className={`h-4 w-4 text-gray-600 transition-transform ${
            expanded ? "rotate-180" : ""
          }`}
          fill="none"
          viewBox="0 0 24 24"
          strokeWidth={2}
          stroke="currentColor"
        >
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            d="M19.5 8.25l-7.5 7.5-7.5-7.5"
          />
        </svg>
      </button>

      {expanded && inputStr && (
        <div className="mt-1 rounded-lg bg-gray-800/40 p-3">
          <pre className="overflow-x-auto text-xs text-gray-500 whitespace-pre-wrap break-all font-mono">
            {inputStr}
          </pre>
        </div>
      )}
    </div>
  );
}
