import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { TurnBlock } from "../../hooks/useAgentStream";
import AssistantTurn from "../AssistantTurn";

describe("AssistantTurn", () => {
  it("shows a trace toggle above plain history responses", () => {
    render(<AssistantTurn blocks={[]} fallbackContent="Plain reply" />);

    expect(
      screen.getByRole("button", {
        name: "Show trace: no reasoning or tool use recorded",
      }),
    ).toBeInTheDocument();
    expect(screen.getByText("Plain reply")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Show trace/ }));

    expect(
      screen.getByRole("button", {
        name: "Hide trace: no reasoning or tool use recorded",
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByText("No reasoning or tool use was recorded for this response."),
    ).toBeInTheDocument();
    expect(screen.getByText("Plain reply")).toBeInTheDocument();
  });

  it("hides reasoning and tool use until the trace toggle is opened", () => {
    const blocks: TurnBlock[] = [
      { id: "thinking-1", type: "thinking", text: "Inspect files." },
      {
        id: "tool-1",
        type: "tool_use",
        toolName: "read",
        toolInput: { path: "README.md" },
        toolOutput: "# Project",
      },
      { id: "text-1", type: "text", text: "Done." },
    ];

    render(<AssistantTurn blocks={blocks} />);

    expect(
      screen.getByRole("button", { name: "Show trace: 1 tool call, reasoning" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Done.")).toBeInTheDocument();
    expect(screen.queryByText("Inspect files.")).not.toBeInTheDocument();
    expect(screen.queryByText("read")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Show trace/ }));

    expect(
      screen.getByRole("button", { name: "Hide trace: 1 tool call, reasoning" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Inspect files.")).toBeInTheDocument();
    expect(screen.getByText("read")).toBeInTheDocument();
    expect(screen.getByText("Done.")).toBeInTheDocument();
  });

  it("keeps error blocks visible when the trace panel is collapsed", () => {
    const blocks: TurnBlock[] = [
      { id: "error-1", type: "error", text: "Stream failed" },
    ];

    render(<AssistantTurn blocks={blocks} />);

    expect(
      screen.getByRole("button", {
        name: "Show trace: no reasoning or tool use recorded",
      }),
    ).toBeInTheDocument();
    expect(screen.getByText("Stream failed")).toBeInTheDocument();
  });
});
