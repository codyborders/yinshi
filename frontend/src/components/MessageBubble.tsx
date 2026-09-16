import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { ChatAttachment } from "../hooks/useAgentStream";

interface MessageBubbleProps {
  role: string;
  content: string;
  streaming?: boolean;
  attachments?: ChatAttachment[];
}

function bubbleStyle(role: string): string {
  if (role === "user") {
    return "bg-blue-500 text-white rounded-br-md";
  }
  if (role === "error") {
    return "bg-red-500/20 text-red-300 rounded-bl-md";
  }
  return "bg-gray-800 text-gray-200 rounded-bl-md";
}

export default function MessageBubble({
  role,
  content,
  streaming,
  attachments = [],
}: MessageBubbleProps) {
  const isUser = role === "user";

  return (
    <div
      className={`animate-message-in flex ${isUser ? "justify-end" : "justify-start"}`}
    >
      <div
        className={`max-w-[85%] rounded-2xl px-4 py-2.5 text-sm ${bubbleStyle(role)}`}
      >
        {isUser ? (
          <div className="whitespace-pre-wrap break-words leading-relaxed">
            {content}
          </div>
        ) : (
          <div className="markdown-prose break-words">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>
              {content}
            </ReactMarkdown>
          </div>
        )}
        {attachments.length > 0 && (
          <div className="mt-2 flex flex-wrap gap-1 border-t border-white/20 pt-2">
            {attachments.map((attachment) => (
              <span key={attachment.id} className="max-w-52 truncate rounded bg-black/20 px-2 py-1 text-xs">
                {attachment.filename}
              </span>
            ))}
          </div>
        )}
        {streaming && (
          <span className="inline-block h-3 w-0.5 animate-pulse bg-blue-400 ml-0.5 align-middle" />
        )}
      </div>
    </div>
  );
}
