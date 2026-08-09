import { expect, it } from "vitest";

import { bootstrapHelperSession } from "./helperBootstrap.js";

it("exchanges the inherited nonce through the Electron session before loading helper UI", async () => {
  const calls: Array<{ url: string; init: RequestInit | undefined }> = [];
  const origin = await bootstrapHelperSession({
    ready: {
      port: 43123,
      instanceNonce: "a".repeat(43),
    },
    fetch: async (input, init) => {
      const url = input.toString();
      calls.push({ url, init });
      if (url.endsWith("/desktop/bootstrap")) {
        return new Response(null, {
          status: 204,
          headers: {
            "Set-Cookie":
              "yinshi_desktop_session=session-token; HttpOnly; Path=/; SameSite=Strict",
          },
        });
      }
      return new Response(JSON.stringify({ status: "ok" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    },
  });

  expect(origin).toBe("http://127.0.0.1:43123");
  expect(calls.map((call) => call.url)).toEqual([
    "http://127.0.0.1:43123/desktop/bootstrap",
    "http://127.0.0.1:43123/health",
  ]);
  expect(calls[0]?.init?.method).toBe("POST");
  expect(calls[0]?.init?.body).toBeUndefined();
  expect(calls[0]?.init?.headers).toEqual({
    "X-Yinshi-Bootstrap": "a".repeat(43),
  });
  expect(calls[1]?.init?.headers).toBeUndefined();
});
