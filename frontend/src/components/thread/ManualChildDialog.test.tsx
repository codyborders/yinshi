import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import ManualChildDialog from "./ManualChildDialog";

describe("ManualChildDialog", () => {
  it("renders labelled task fields when open", () => {
    render(<ManualChildDialog open onClose={() => {}} onSubmit={() => {}} />);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByLabelText("Title")).toBeInTheDocument();
    expect(screen.getByLabelText("Task")).toBeInTheDocument();
  });

  it("submits one bounded child request", async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    vi.spyOn(crypto, "randomUUID").mockReturnValue(
      "123e4567-e89b-42d3-a456-426614174000",
    );
    render(<ManualChildDialog open onClose={() => {}} onSubmit={onSubmit} />);

    fireEvent.change(screen.getByLabelText("Title"), {
      target: { value: "Check parser" },
    });
    fireEvent.change(screen.getByLabelText("Task"), {
      target: { value: "Review parser edge cases" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create child" }));

    expect(onSubmit).toHaveBeenCalledWith(
      expect.objectContaining({
        idempotency_key: "123e4567-e89b-42d3-a456-426614174000",
        title: "Check parser",
        task: "Review parser edge cases",
        role: "general",
        start_immediately: true,
      }),
    );
  });

  it("closes when Escape is pressed", () => {
    const onClose = vi.fn();
    render(<ManualChildDialog open onClose={onClose} onSubmit={() => {}} />);

    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });

    expect(onClose).toHaveBeenCalledOnce();
  });

  it("includes optional context, role, model, thinking, and start settings", () => {
    const onSubmit = vi.fn();
    vi.spyOn(crypto, "randomUUID").mockReturnValue(
      "123e4567-e89b-42d3-a456-426614174000",
    );
    render(<ManualChildDialog open onClose={() => {}} onSubmit={onSubmit} />);

    fireEvent.change(screen.getByLabelText("Title"), {
      target: { value: "Check parser" },
    });
    fireEvent.change(screen.getByLabelText("Task"), {
      target: { value: "Review parser edge cases" },
    });
    fireEvent.change(screen.getByLabelText("Context"), {
      target: { value: "Focus on malformed input." },
    });
    fireEvent.change(screen.getByLabelText("Role"), {
      target: { value: "implementation" },
    });
    fireEvent.change(screen.getByLabelText("Model"), {
      target: { value: "provider/model" },
    });
    fireEvent.change(screen.getByLabelText("Thinking"), {
      target: { value: "high" },
    });
    fireEvent.click(screen.getByLabelText("Start immediately"));
    fireEvent.click(screen.getByRole("button", { name: "Create child" }));

    expect(onSubmit).toHaveBeenCalledWith({
      idempotency_key: "123e4567-e89b-42d3-a456-426614174000",
      title: "Check parser",
      task: "Review parser edge cases",
      context: "Focus on malformed input.",
      role: "implementation",
      model: "provider/model",
      thinking: "high",
      start_immediately: false,
    });
  });

  it("restores focus to the opener when closed", () => {
    const opener = document.createElement("button");
    opener.textContent = "Open child dialog";
    document.body.appendChild(opener);
    opener.focus();
    const { rerender } = render(
      <ManualChildDialog open={false} onClose={() => {}} onSubmit={() => {}} />,
    );

    rerender(
      <ManualChildDialog open onClose={() => {}} onSubmit={() => {}} />,
    );
    rerender(
      <ManualChildDialog open={false} onClose={() => {}} onSubmit={() => {}} />,
    );

    expect(opener).toHaveFocus();
    opener.remove();
  });

  it("wraps keyboard focus within the open dialog", () => {
    render(<ManualChildDialog open onClose={() => {}} onSubmit={() => {}} />);
    const first = screen.getByLabelText("Title");
    const last = screen.getByRole("button", { name: "Create child" });
    last.focus();

    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Tab" });

    expect(first).toHaveFocus();
  });

  it("closes when the backdrop is clicked", () => {
    const onClose = vi.fn();
    render(<ManualChildDialog open onClose={onClose} onSubmit={() => {}} />);

    fireEvent.click(screen.getByTestId("child-dialog-backdrop"));

    expect(onClose).toHaveBeenCalledOnce();
  });

  it("closes from the cancel button", () => {
    const onClose = vi.fn();
    render(<ManualChildDialog open onClose={onClose} onSubmit={() => {}} />);

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(onClose).toHaveBeenCalledOnce();
  });

  it("shows server errors in an alert", () => {
    render(
      <ManualChildDialog
        open
        onClose={() => {}}
        onSubmit={() => {}}
        serverError="Unable to create child thread."
      />,
    );

    expect(screen.getByRole("alert")).toHaveTextContent(
      "Unable to create child thread.",
    );
  });

  it("prevents duplicate submits while the request is settling", () => {
    const onSubmit = vi.fn().mockImplementation(() => new Promise(() => {}));
    render(<ManualChildDialog open onClose={() => {}} onSubmit={onSubmit} />);

    fireEvent.change(screen.getByLabelText("Title"), {
      target: { value: "Check parser" },
    });
    fireEvent.change(screen.getByLabelText("Task"), {
      target: { value: "Review parser edge cases" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create child" }));
    fireEvent.click(screen.getByRole("button", { name: "Create child" }));

    expect(onSubmit).toHaveBeenCalledOnce();
  });

  it("disables submit and explains when child capacity is reached", () => {
    render(
      <ManualChildDialog
        open
        onClose={() => {}}
        onSubmit={() => {}}
        limits={{
          max_depth: 1,
          max_direct_children: 1,
          max_active_descendants: 1,
          max_total_threads: 2,
          tree_depth: 0,
          direct_children: 1,
          active_descendants: 1,
          total_threads: 1,
          can_spawn_child: false,
        }}
      />,
    );

    expect(screen.getByText(/child capacity/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create child" })).toBeDisabled();
  });

  it("focuses the title when opened", () => {
    render(<ManualChildDialog open onClose={() => {}} onSubmit={() => {}} />);
    expect(screen.getByLabelText("Title")).toHaveFocus();
  });
});
