import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import ThreadStatusBadge from "./ThreadStatusBadge";

describe("ThreadStatusBadge", () => {
  it("renders readable text for every lifecycle state", () => {
    const states = [
      ["provisioning", "Provisioning"],
      ["queued", "Queued"],
      ["running", "Running"],
      ["cancelling", "Cancelling"],
      ["completed", "Completed"],
      ["failed", "Failed"],
      ["cancelled", "Cancelled"],
      ["interrupted", "Interrupted"],
    ] as const;

    for (const [state, label] of states) {
      const { unmount } = render(<ThreadStatusBadge state={state} />);
      expect(screen.getByText(label)).toBeInTheDocument();
      unmount();
    }
  });
});
