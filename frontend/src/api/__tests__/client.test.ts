import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, api } from "../client";

describe("api client", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("parses structured GitHub access errors", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            detail: {
              code: "github_connect_required",
              message: "Connect GitHub to import this private repository.",
              connect_url: "/auth/github/install",
              manage_url: null,
            },
          }),
          {
            status: 400,
            headers: { "Content-Type": "application/json" },
          },
        ),
      ),
    );

    let caughtError: unknown;
    try {
      await api.get("/api/repos");
    } catch (error) {
      caughtError = error;
    }

    expect(caughtError).toBeInstanceOf(ApiError);
    expect(caughtError).toMatchObject({
      code: "github_connect_required",
      connectUrl: "/auth/github/install",
      manageUrl: null,
      message: "Connect GitHub to import this private repository.",
      status: 400,
    });
  });
});
