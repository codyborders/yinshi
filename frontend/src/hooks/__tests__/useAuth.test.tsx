import { render, screen, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { AuthProvider, useAuth } from "../useAuth";
import { MemoryRouter } from "react-router-dom";

function AuthStatus() {
  const { status, email } = useAuth();
  return (
    <div>
      <span data-testid="status">{status}</span>
      <span data-testid="email">{email ?? "none"}</span>
    </div>
  );
}

function renderWithAuth(initialEntry = "/") {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <AuthProvider>
        <AuthStatus />
      </AuthProvider>
    </MemoryRouter>,
  );
}

describe("useAuth", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    Reflect.deleteProperty(window, "yinshiDesktop");
  });

  it("uses the active desktop profile without calling the helper auth route", async () => {
    const fetch = vi.spyOn(globalThis, "fetch");
    Object.defineProperty(window, "yinshiDesktop", {
      configurable: true,
      value: {
        listProfiles: vi.fn().mockResolvedValue([
          {
            user: { id: "desktop-user", email: "desktop@example.com" },
            hasCredentials: true,
            active: true,
          },
        ]),
      },
    });

    renderWithAuth();

    await waitFor(() => {
      expect(screen.getByTestId("status").textContent).toBe("authenticated");
      expect(screen.getByTestId("email").textContent).toBe(
        "desktop@example.com",
      );
    });
    expect(fetch).not.toHaveBeenCalled();
  });

  it("sets status to authenticated when /auth/me returns authenticated: true", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      new Response(
        JSON.stringify({ authenticated: true, email: "user@test.com" }),
        {
          status: 200,
          headers: { "Content-Type": "application/json" },
        },
      ),
    );

    renderWithAuth();

    await waitFor(() => {
      expect(screen.getByTestId("status").textContent).toBe("authenticated");
      expect(screen.getByTestId("email").textContent).toBe("user@test.com");
    });
  });

  it("sets status to unauthenticated on 401", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      new Response("Not authenticated", { status: 401 }),
    );

    renderWithAuth();

    await waitFor(() => {
      expect(screen.getByTestId("status").textContent).toBe("unauthenticated");
    });
  });

  it("sets status to unauthenticated when authenticated: false", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      new Response(JSON.stringify({ authenticated: false }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );

    renderWithAuth();

    await waitFor(() => {
      expect(screen.getByTestId("status").textContent).toBe("unauthenticated");
    });
  });

  it("sets status to disabled only for explicit no-auth mode", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      new Response(
        JSON.stringify({ authenticated: false, auth_disabled: true }),
        {
          status: 200,
          headers: { "Content-Type": "application/json" },
        },
      ),
    );

    renderWithAuth();

    await waitFor(() => {
      expect(screen.getByTestId("status").textContent).toBe("disabled");
    });
  });

  it("sets status to unauthenticated on network error", async () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValueOnce(
      new Error("Network error"),
    );

    renderWithAuth();

    await waitFor(() => {
      expect(screen.getByTestId("status").textContent).toBe("unauthenticated");
    });
  });

  it("shows loading spinner initially", () => {
    vi.spyOn(globalThis, "fetch").mockReturnValueOnce(new Promise(() => {}));

    const { container } = render(
      <MemoryRouter>
        <AuthProvider>
          <div data-testid="child">child</div>
        </AuthProvider>
      </MemoryRouter>,
    );

    expect(container.querySelector(".animate-spin")).toBeTruthy();
    expect(screen.queryByTestId("child")).toBeNull();
  });
});
