import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import ThreadNode from "./ThreadNode";
import type { ThreadNodeOut } from "../../api/client";

const node: ThreadNodeOut = {
  id: "child-1", delegation_id: "delegation-1", parent_id: "root", root_id: "root", depth: 9,
  title: null, role: "implementation", origin: "delegated", state: "running", workspace_id: "workspace",
  model: "model", child_count: 0, active_child_count: 0, can_spawn_child: true, created_at: "2026-01-01T00:00:00Z",
};

describe("ThreadNode", () => {
  it("uses an accessible navigation button with role fallback and capped indentation", () => {
    render(<ThreadNode node={node} current />);
    const button = screen.getByRole("button", { name: "Open Implementation thread" });
    expect(button).toHaveAttribute("aria-current", "page");
    expect(button.parentElement).toHaveStyle({ paddingInlineStart: "6rem" });
  });
});
