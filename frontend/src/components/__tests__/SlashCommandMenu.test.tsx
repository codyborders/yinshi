import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import SlashCommandMenu, { type SlashCommand } from "../SlashCommandMenu";

const COMMANDS: SlashCommand[] = [
  { name: "help", description: "List available commands", source: "builtin" },
  { name: "model", description: "Show or change the AI model", source: "builtin" },
  { name: "tree", description: "Show workspace file tree", source: "builtin" },
  { name: "export", description: "Download chat as markdown", source: "builtin" },
  { name: "clear", description: "Clear chat display", source: "builtin" },
];

describe("SlashCommandMenu", () => {
  it("renders all commands when filter is empty", () => {
    render(
      <SlashCommandMenu
        filter=""
        commands={COMMANDS}
        selectedIndex={0}
        onSelect={vi.fn()}
      />,
    );
    expect(screen.getByText("/help")).toBeTruthy();
    expect(screen.getByText("/model")).toBeTruthy();
    expect(screen.getByText("/tree")).toBeTruthy();
    expect(screen.getByText("/export")).toBeTruthy();
    expect(screen.getByText("/clear")).toBeTruthy();
  });

  it("filters commands by name", () => {
    render(
      <SlashCommandMenu
        filter="mod"
        commands={COMMANDS}
        selectedIndex={0}
        onSelect={vi.fn()}
      />,
    );
    expect(screen.getByText("/model")).toBeTruthy();
    expect(screen.queryByText("/help")).toBeNull();
    expect(screen.queryByText("/tree")).toBeNull();
  });

  it("highlights the selected index", () => {
    render(
      <SlashCommandMenu
        filter=""
        commands={COMMANDS}
        selectedIndex={1}
        onSelect={vi.fn()}
      />,
    );
    const items = screen.getAllByRole("option");
    expect(items[1].getAttribute("aria-selected")).toBe("true");
    expect(items[0].getAttribute("aria-selected")).toBe("false");
  });

  it("calls onSelect when a command is clicked", () => {
    const onSelect = vi.fn();
    render(
      <SlashCommandMenu
        filter=""
        commands={COMMANDS}
        selectedIndex={0}
        onSelect={onSelect}
      />,
    );
    const items = screen.getAllByRole("option");
    // /tree is the third command (index 2)
    fireEvent.mouseDown(items[2]);
    expect(onSelect).toHaveBeenCalledWith(COMMANDS[2]);
  });

  it("returns null when no commands match filter", () => {
    const { container } = render(
      <SlashCommandMenu
        filter="zzz"
        commands={COMMANDS}
        selectedIndex={0}
        onSelect={vi.fn()}
      />,
    );
    expect(container.innerHTML).toBe("");
  });
});
