import { act, screen } from "@testing-library/react";
import type { HTMLAttributes, PropsWithChildren } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const preloadEncryptedRunnerCryptoMock = vi.fn();

vi.mock("./runner/encryptedRunnerClient", () => ({
  preloadEncryptedRunnerCrypto: () => preloadEncryptedRunnerCryptoMock(),
}));

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
    window.history.replaceState(null, "", "/app/session/test");
    vi.stubGlobal("requestIdleCallback", vi.fn());
  });

  it("renders app paths when runner crypto preload rejects without an unhandled rejection", async () => {
    const consoleError = vi
      .spyOn(console, "error")
      .mockImplementation(() => undefined);
    const unhandledRejection = vi.fn();
    window.addEventListener("unhandledrejection", unhandledRejection);
    preloadEncryptedRunnerCryptoMock.mockRejectedValue(
      new Error("preload unavailable"),
    );

    await act(async () => {
      await import("./main");
      await Promise.resolve();
    });

    expect(preloadEncryptedRunnerCryptoMock).toHaveBeenCalledOnce();
    expect(await screen.findByRole("main")).toHaveTextContent(
      "Yinshi application",
    );
    expect(unhandledRejection).not.toHaveBeenCalled();
    expect(consoleError).not.toHaveBeenCalled();
    window.removeEventListener("unhandledrejection", unhandledRejection);
  });
});
