import { memo, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { TurnBlock } from "../hooks/useAgentStream";
import StreamingDots from "./StreamingDots";
import ThinkingBlock from "./ThinkingBlock";
import ToolCallBlock, { ToolIcon } from "./ToolCallBlock";

interface AssistantTurnProps {
  blocks: TurnBlock[];
  streaming?: boolean;
  /** Fallback plain text when no blocks (e.g., loaded from history) */
  fallbackContent?: string;
}

function turnSummary(blocks: TurnBlock[]) {
  const tools = blocks.filter((b) => b.type === "tool_use");
  const thinking = blocks.filter((b) => b.type === "thinking");
  const errors = blocks.filter(
    (b) => b.type === "error" || b.toolError,
  );

  const toolNames = [...new Set(tools.map((t) => t.toolName || ""))];

  const parts: string[] = [];
  if (tools.length > 0) {
    parts.push(`${tools.length} tool call${tools.length !== 1 ? "s" : ""}`);
  }
  if (thinking.length > 0) {
    parts.push("reasoning");
  }
  if (errors.length > 0) {
    parts.push(`${errors.length} error${errors.length !== 1 ? "s" : ""}`);
  }

  return { text: parts.join(", "), toolNames, toolCount: tools.length };
}

const AssistantTurn = memo(function AssistantTurn({
  blocks,
  streaming,
  fallbackContent,
}: AssistantTurnProps) {
  const [collapsed, setCollapsed] = useState(false);
  const summary = turnSummary(blocks);
  const hasDetails = blocks.some(
    (b) => b.type === "tool_use" || b.type === "thinking",
  );

  // Fallback for history messages with no blocks
  if (blocks.length === 0 && fallbackContent) {
    return (
      <div className="animate-message-in px-2 py-1">
        <div className="markdown-prose text-sm text-gray-200">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>
            {fallbackContent}
          </ReactMarkdown>
        </div>
      </div>
    );
  }

  return (
    <div className="animate-message-in space-y-1">
      {/* Turn summary header */}
      {hasDetails && (
        <button
          onClick={() => setCollapsed(!collapsed)}
          className="flex items-center gap-2 rounded-lg px-2 py-1 text-xs text-gray-500 hover:bg-gray-800/50 hover:text-gray-400"
        >
          <svg
            className={`h-3 w-3 shrink-0 transition-transform ${collapsed ? "-rotate-90" : ""}`}
            fill="none"
            viewBox="0 0 24 24"
            strokeWidth={2}
            stroke="currentColor"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              d="m19.5 8.25-7.5 7.5-7.5-7.5"
            />
          </svg>
          <span>{summary.text}</span>
          {summary.toolNames.length > 0 && (
            <span className="flex gap-1 text-gray-600">
              {summary.toolNames.map((name) => (
                <ToolIcon key={name} name={name} />
              ))}
            </span>
          )}
          {streaming && (
            <span className="ml-1">
              <StreamingDots size="sm" />
            </span>
          )}
        </button>
      )}

      {/* Blocks */}
      {!collapsed &&
        blocks.map((block) => {
          switch (block.type) {
            case "text":
              return (
                <div
                  key={block.id}
                  className="markdown-prose px-2 text-sm text-gray-200"
                >
                  <ReactMarkdown remarkPlugins={[remarkGfm]}>
                    {block.text || ""}
                  </ReactMarkdown>
                </div>
              );
            case "thinking":
              return <ThinkingBlock key={block.id} text={block.text || ""} />;
            case "tool_use":
              return (
                <ToolCallBlock
                  key={block.id}
                  toolName={block.toolName || "unknown"}
                  toolInput={block.toolInput}
                  toolOutput={block.toolOutput}
                  toolError={block.toolError}
                />
              );
            case "error":
              return (
                <div
                  key={block.id}
                  className="mx-2 rounded-lg bg-red-900/30 border border-red-800/50 px-3 py-2 text-sm text-red-300"
                >
                  <div className="flex items-center gap-2">
                    <svg
                      className="h-4 w-4 shrink-0"
                      fill="none"
                      viewBox="0 0 24 24"
                      strokeWidth={2}
                      stroke="currentColor"
                    >
                      <path
                        strokeLinecap="round"
                        strokeLinejoin="round"
                        d="M12 9v3.75m9-.75a9 9 0 1 1-18 0 9 9 0 0 1 18 0Zm-9 3.75h.008v.008H12v-.008Z"
                      />
                    </svg>
                    <span>{block.text}</span>
                  </div>
                </div>
              );
            default:
              return null;
          }
        })}

      {/* Streaming cursor when no summary header */}
      {streaming && !hasDetails && (
        <div className="flex items-center gap-1 px-2 py-1">
          <StreamingDots />
        </div>
      )}
    </div>
  );
});

export default AssistantTurn;
