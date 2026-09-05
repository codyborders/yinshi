import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import ThreadPanel from "./ThreadPanel";
import type { ThreadTreeOut } from "../../api/client";

const tree: ThreadTreeOut = {
  root: {
    id: "root", delegation_id: null, parent_id: null, root_id: "root", depth: 0,
    title: "Root", role: "general", origin: "root", state: "running", workspace_id: "workspace",
    model: "model", child_count: 3, active_child_count: 2, can_spawn_child: true,
    created_at: "2026-01-01T00:00:00Z",
  },
  nodes: [
    {
      id: "child-1", delegation_id: "delegation-1", parent_id: "root", root_id: "root", depth: 1,
      title: "Attached child", role: "implementation", origin: "delegated", state: "failed", workspace_id: "workspace",
      model: "model", child_count: 0, active_child_count: 0, can_spawn_child: true,
      created_at: "2026-01-01T00:00:00Z",
    },
  ],
  placeholders: [{ delegation_id: "delegation-2", parent_id: "root", title: "Starting child", role: "test", status: "running", created_at: "2026-01-01T00:00:00Z" }],
  thread_count: 2, active_descendant_count: 2, tree_depth: 1,
};

describe("ThreadPanel", () => {
  it("shows loading state", () => {
    render(<ThreadPanel tree={null} loading />);
    expect(screen.getByRole("status")).toHaveTextContent("Loading thread tree...");
  });

  it("renders root, attached nodes, and placeholders", () => {
    render(<ThreadPanel tree={tree} />);
    expect(screen.getByRole("button", { name: "Open Root" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Open Attached child" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Open Starting child" })).toBeInTheDocument();
  });

  it("shows actionable errors", () => {
    render(<ThreadPanel tree={null} error="Unable to load threads." />);
    expect(screen.getByRole("alert")).toHaveTextContent("Unable to load threads.");
  });

  it("delegates navigation, cancel, and retry actions with API identifiers", () => {
    const onNavigate = vi.fn();
    const onCancel = vi.fn();
    const onRetry = vi.fn();
    render(<ThreadPanel tree={tree} currentThreadId="child-1" onNavigate={onNavigate} onCancel={onCancel} onRetry={onRetry} />);
    fireEvent.click(screen.getByRole("button", { name: "Open Attached child" }));
    fireEvent.click(screen.getByRole("button", { name: "Cancel Starting child" }));
    fireEvent.click(screen.getByRole("button", { name: "Retry Attached child" }));
    expect(onNavigate).toHaveBeenCalledWith("child-1");
    expect(onCancel).toHaveBeenCalledWith("delegation-2");
    expect(onRetry).toHaveBeenCalledWith("child-1");
    expect(screen.getByRole("button", { name: "Open Attached child" })).toHaveAttribute("aria-current", "page");
  });

  it("opens child dialog from create button", () => {
    const onCreateChild = vi.fn();
    render(<ThreadPanel tree={tree} onCreateChild={onCreateChild} />);
    fireEvent.click(screen.getByRole("button", { name: "Create child thread" }));
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("keeps the create dialog open when creation returns no child", async () => {
    const onCreateChild = vi.fn().mockResolvedValue(null);
    const view = render(<ThreadPanel tree={tree} onCreateChild={onCreateChild} />);
    fireEvent.click(screen.getByRole("button", { name: "Create child thread" }));
    fireEvent.change(screen.getByLabelText("Title"), { target: { value: "Review parser" } });
    fireEvent.change(screen.getByLabelText("Task"), { target: { value: "Check parser errors" } });
    fireEvent.click(screen.getByRole("button", { name: "Create child" }));

    await waitFor(() => expect(onCreateChild).toHaveBeenCalledOnce());
    view.rerender(
      <ThreadPanel tree={tree} error="Child limit reached." onCreateChild={onCreateChild} />,
    );
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("Child limit reached.");
  });

  it("renders current thread result", () => {
    render(
      <ThreadPanel
        tree={tree}
        currentThreadId="child-1"
        result={{
          delegation_id: "delegation-1",
          version: 1,
          source: "reported",
          sealed: true,
          summary: "Child finished.",
          tests: [],
          warnings: [],
          base_commit: null,
          result_commit: null,
          result_ref: null,
          changed_files: [],
          created_at: "2026-01-01T00:00:00Z",
          updated_at: "2026-01-01T00:01:00Z",
          sealed_at: "2026-01-01T00:01:00Z",
        }}
      />,
    );
    expect(screen.getByTestId("thread-result")).toHaveTextContent("Child finished.");
  });

  it("shows empty state without a tree", () => {
    render(<ThreadPanel tree={null} loading={false} />);
    expect(screen.getByText("No thread tree available.")).toBeInTheDocument();
  });
});
