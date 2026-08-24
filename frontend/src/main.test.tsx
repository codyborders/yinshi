import { act, screen } from "@testing-library/react";
import type { HTMLAttributes, PropsWithChildren } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("react-router-dom", () => ({
  BrowserRouter: (props: PropsWithChildren<HTMLAttributes<HTMLDivElement>>) => (
    <div {...props} />
  ),
}));

vi.mock("@datadog/browser-rum", () => ({
  datadogRum: { init: vi.fn() },
}));

vi.mock("./App", () => ({
  default: () => <main>Yinshi application</main>,
}));

vi.mock("./hooks/useAuth", () => ({
  AuthProvider: ({ children }: PropsWithChildren) => children,
}));

describe("application bootstrap", () => {
  beforeEach(() => {
    document.body.innerHTML = '<div id="root"></div>';
    vi.stubGlobal("requestIdleCallback", vi.fn());
  });

  it("renders the application", async () => {
    await act(async () => {
      await import("./main");
    });

    expect(await screen.findByRole("main")).toHaveTextContent(
      "Yinshi application",
    );
  });
});
