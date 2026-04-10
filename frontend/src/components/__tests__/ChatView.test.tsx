import { fireEvent, render, screen } from "@testing-library/react";
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
