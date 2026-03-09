interface MessageBubbleProps {
  role: string;
  content: string;
  streaming?: boolean;
}

export default function MessageBubble({
  role,
  content,
  streaming,
}: MessageBubbleProps) {
  const isUser = role === "user";
  const isError = role === "error";

  return (
    <div
      className={`animate-message-in flex ${isUser ? "justify-end" : "justify-start"}`}
    >
      <div
        className={`max-w-[85%] rounded-2xl px-4 py-2.5 text-sm leading-relaxed ${
          isUser
            ? "bg-blue-500 text-white"
            : isError
              ? "bg-red-500/20 text-red-300"
              : "bg-gray-800 text-gray-200"
        } ${
          isUser
            ? "rounded-br-md"
            : "rounded-bl-md"
        }`}
      >
        <div className="whitespace-pre-wrap break-words">{content}</div>
        {streaming && (
          <span className="inline-block h-3 w-0.5 animate-pulse bg-blue-400 ml-0.5 align-middle" />
        )}
      </div>
    </div>
  );
}
