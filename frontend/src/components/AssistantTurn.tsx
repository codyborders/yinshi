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

function isTraceBlock(block: TurnBlock): boolean {
  return block.type === "tool_use" || block.type === "thinking";
}

function isResponseBlock(block: TurnBlock): boolean {
  return block.type === "text" || block.type === "error";
}

function turnSummary(blocks: TurnBlock[]) {
  const tools = blocks.filter((block) => block.type === "tool_use");
  const thinking = blocks.filter((block) => block.type === "thinking");
  const toolNames = [
    ...new Set(
      tools
        .map((tool) => tool.toolName || "")
        .filter((toolName) => toolName.length > 0),
    ),
  ];

  const parts: string[] = [];
  if (tools.length > 0) {
    parts.push(`${tools.length} tool call${tools.length !== 1 ? "s" : ""}`);
  }
  if (thinking.length > 0) {
    parts.push("reasoning");
  }

  return {
    text: parts.join(", ") || "no reasoning or tool use recorded",
    toolNames,
  };
}

function renderResponseBlock(block: TurnBlock) {
  switch (block.type) {
    case "text":
      return (
        <div key={block.id} className="markdown-prose px-2 text-sm text-gray-200">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>
            {block.text || ""}
          </ReactMarkdown>
        </div>
      );
    case "error":
      return (
        <div
          key={block.id}
          className="mx-2 rounded-lg border border-red-800/50 bg-red-900/30 px-3 py-2 text-sm text-red-300"
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
}

function renderTraceBlock(block: TurnBlock) {
  switch (block.type) {
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
    default:
      return null;
  }
}

const AssistantTurn = memo(function AssistantTurn({
  blocks,
  streaming,
  fallbackContent,
}: AssistantTurnProps) {
  const [traceExpanded, setTraceExpanded] = useState(false);
  const traceBlocks = blocks.filter(isTraceBlock);
  const responseBlocks = blocks.filter(isResponseBlock);
  const hasTraceBlocks = traceBlocks.length > 0;
  const hasResponseContent = responseBlocks.length > 0 || Boolean(fallbackContent);
  const summary = turnSummary(traceBlocks);

  return (
    <div className="animate-message-in space-y-1">
      <button
        type="button"
        aria-expanded={traceExpanded}
        onClick={() => setTraceExpanded((expanded) => !expanded)}
        className="flex max-w-full items-center gap-2 rounded-lg border border-gray-800 bg-gray-900/70 px-2 py-1 text-left text-xs text-gray-400 transition-colors hover:border-gray-700 hover:bg-gray-800/70 hover:text-gray-200 focus:outline-none focus:ring-1 focus:ring-blue-500"
      >
        <svg
          className={`h-3 w-3 shrink-0 transition-transform ${traceExpanded ? "rotate-180" : "-rotate-90"}`}
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
        <span className="truncate">
          {traceExpanded ? "Hide" : "Show"} trace: {summary.text}
        </span>
        {summary.toolNames.length > 0 && (
          <span className="flex shrink-0 gap-1 text-gray-600">
            {summary.toolNames.map((name) => (
              <ToolIcon key={name} name={name} />
            ))}
          </span>
        )}
        {streaming && (
          <span className="ml-1 shrink-0">
            <StreamingDots size="sm" />
          </span>
        )}
      </button>

      {traceExpanded && (
        <div className="space-y-1">
          {hasTraceBlocks ? (
            traceBlocks.map(renderTraceBlock)
          ) : (
            <div className="mx-2 rounded-lg border border-gray-800/70 bg-gray-900/50 px-3 py-2 text-xs text-gray-500">
              No reasoning or tool use was recorded for this response.
            </div>
          )}
        </div>
      )}

      {responseBlocks.map(renderResponseBlock)}

      {responseBlocks.length === 0 && fallbackContent && (
        <div className="px-2 py-1">
          <div className="markdown-prose text-sm text-gray-200">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>
              {fallbackContent}
            </ReactMarkdown>
          </div>
        </div>
      )}

      {streaming && !hasResponseContent && (
        <div className="flex items-center gap-1 px-2 py-1">
          <StreamingDots />
        </div>
      )}
    </div>
  );
});

export default AssistantTurn;
