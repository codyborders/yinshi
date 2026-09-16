import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import ChatView from "../ChatView";

describe("ChatView", () => {
  it("submits a steering prompt while streaming", () => {
    const onSend = vi.fn();

    render(
      <ChatView
        messages={[]}
        streaming={true}
        onSend={onSend}
        onCancel={vi.fn()}
      />,
    );

    const composer = screen.getByPlaceholderText("Describe what to build...");
    fireEvent.change(composer, { target: { value: "Stop that and fix auth first" } });
    fireEvent.keyDown(composer, { key: "Enter", code: "Enter", shiftKey: false });

    expect(onSend).toHaveBeenCalledWith("Stop that and fix auth first");
  });

  it("uploads and submits a file with the prompt", async () => {
    const onSend = vi.fn();
    const onUpload = vi.fn().mockResolvedValue({
      id: "a".repeat(32),
      filename: "screen.png",
      mediaType: "image/png",
      sizeBytes: 4,
    });
    const { container } = render(
      <ChatView
        messages={[]}
        streaming={false}
        onSend={onSend}
        onCancel={vi.fn()}
        onUpload={onUpload}
      />,
    );
    const input = container.querySelector('input[type="file"]');
    const file = new File(["data"], "screen.png", { type: "image/png" });

    fireEvent.change(input!, { target: { files: [file] } });
    await waitFor(() => expect(screen.getByText("screen.png")).toBeInTheDocument());
    fireEvent.click(screen.getByLabelText("Send"));

    expect(onSend).toHaveBeenCalledWith(
      "Please inspect the attached files.",
      [expect.objectContaining({ filename: "screen.png" })],
    );
  });

  it("clamps slash selection after caret movement changes filtered commands", () => {
    render(
      <ChatView
        messages={[]}
        streaming={false}
        onSend={vi.fn()}
        onCancel={vi.fn()}
      />,
    );

    const composer = screen.getByPlaceholderText("Describe what to build...");
    fireEvent.change(composer, {
      target: { value: "/ /h", selectionStart: 1, selectionEnd: 1 },
    });
    fireEvent.keyDown(composer, { key: "ArrowDown" });
    fireEvent.keyDown(composer, { key: "ArrowDown" });
    fireEvent.keyDown(composer, { key: "ArrowDown" });
    fireEvent.keyDown(composer, { key: "ArrowDown" });
    fireEvent.select(composer, {
      target: { selectionStart: 4, selectionEnd: 4 },
    });
    fireEvent.keyDown(composer, { key: "Enter" });

    expect(composer).toHaveValue("/ /help ");
  });

  it("replaces complete slash token when caret is inside token", () => {
    render(
      <ChatView
        messages={[]}
        streaming={false}
        onSend={vi.fn()}
        onCancel={vi.fn()}
      />,
    );

    const composer = screen.getByPlaceholderText("Describe what to build...");
    fireEvent.change(composer, {
      target: {
        value: "say /heXYZ then",
        selectionStart: 7,
        selectionEnd: 7,
      },
    });
    fireEvent.keyDown(composer, { key: "Enter" });

    expect(composer).toHaveValue("say /help then");
  });

  it("shows cancel when streaming without pending steering input", () => {
    render(
      <ChatView
        messages={[]}
        streaming={true}
        onSend={vi.fn()}
        onCancel={vi.fn()}
      />,
    );

    expect(screen.getByLabelText("Cancel")).toBeInTheDocument();
    expect(screen.queryByLabelText("Steer")).not.toBeInTheDocument();
  });
});
