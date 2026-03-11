import { render, screen } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import RequireAuth from "../RequireAuth";

vi.mock("../../hooks/useAuth", () => ({
  useAuth: vi.fn(),
}));

import { useAuth } from "../../hooks/useAuth";
const mockUseAuth = vi.mocked(useAuth);

function renderWithRouter(initialEntry: string) {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <Routes>
        <Route path="/login" element={<div data-testid="login">Login</div>} />
        <Route element={<RequireAuth />}>
          <Route path="/" element={<div data-testid="home">Home</div>} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
}

describe("RequireAuth", () => {
  it("renders children when authenticated", () => {
    mockUseAuth.mockReturnValue({ status: "authenticated", email: "u@t.com", logout: vi.fn() });
    renderWithRouter("/");
    expect(screen.getByTestId("home")).toBeTruthy();
  });

  it("renders children when auth is disabled", () => {
    mockUseAuth.mockReturnValue({ status: "disabled", email: null, logout: vi.fn() });
    renderWithRouter("/");
    expect(screen.getByTestId("home")).toBeTruthy();
  });

  it("redirects to /login when unauthenticated", () => {
    mockUseAuth.mockReturnValue({ status: "unauthenticated", email: null, logout: vi.fn() });
    renderWithRouter("/");
    expect(screen.getByTestId("login")).toBeTruthy();
    expect(screen.queryByTestId("home")).toBeNull();
  });
});
