import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";

vi.mock("../../hooks/useAuth", () => ({
  useAuth: vi.fn(() => ({
    status: "authenticated",
    email: "u@t.com",
    logout: vi.fn(),
  })),
}));

vi.mock("../../api/client", () => ({
  api: {
    get: vi.fn(() => Promise.resolve([])),
    post: vi.fn(() => Promise.resolve({})),
  },
}));

import Layout from "../Layout";

function renderLayout() {
  return render(
    <MemoryRouter initialEntries={["/"]}>
      <Routes>
        <Route element={<Layout />}>
          <Route
            path="/"
            element={<div data-testid="content">Content</div>}
          />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
}

describe("Layout", () => {
  it("renders sidebar and main content", () => {
    renderLayout();
    expect(screen.getByTestId("content")).toBeTruthy();
    expect(screen.getByText("Workspaces")).toBeTruthy();
  });

  it("renders a mobile menu toggle button", () => {
    renderLayout();
    expect(screen.getByLabelText("Toggle sidebar")).toBeTruthy();
  });

  it("sidebar panel is hidden by default on mobile", () => {
    renderLayout();
    const panel = screen.getByTestId("sidebar-panel");
    expect(panel.className).toContain("-translate-x-full");
    expect(panel.className).toContain("md:translate-x-0");
  });

  it("opens sidebar and shows overlay when toggle is clicked", () => {
    renderLayout();
    fireEvent.click(screen.getByLabelText("Toggle sidebar"));
    const panel = screen.getByTestId("sidebar-panel");
    expect(panel.className).toContain("translate-x-0");
    expect(panel.className).not.toContain("-translate-x-full");
    expect(screen.getByTestId("sidebar-overlay")).toBeTruthy();
  });

  it("closes sidebar when overlay is clicked", () => {
    renderLayout();
    fireEvent.click(screen.getByLabelText("Toggle sidebar"));
    expect(screen.getByTestId("sidebar-overlay")).toBeTruthy();
    fireEvent.click(screen.getByTestId("sidebar-overlay"));
    expect(screen.queryByTestId("sidebar-overlay")).toBeNull();
  });
});
