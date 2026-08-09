import { expect, it } from "vitest";

import { startAuthCallbackListener } from "./authCallbackListener.js";

it("captures one exact loopback callback and closes without trusting the Host header", async () => {
  const listener = await startAuthCallbackListener({ timeoutMs: 2_000 });
  expect(listener.callbackUri).toMatch(
    /^http:\/\/127\.0\.0\.1:\d+\/auth\/desktop\/callback$/,
  );

  const callbackPromise = listener.waitForCallback();
  const callbackResponse = await fetch(
    `${listener.callbackUri}?code=${"a".repeat(43)}&state=expected-state`,
    { headers: { Host: "attacker.example" } },
  );
  const callback = await callbackPromise;

  expect(callbackResponse.status).toBe(200);
  expect(callbackResponse.headers.get("content-security-policy")).toBe("default-src 'none'");
  expect(await callbackResponse.text()).toBe(
    "Authentication complete. You can close this window and return to Yinshi.",
  );
  expect(callback.origin).toBe(new URL(listener.callbackUri).origin);
  expect(callback.pathname).toBe("/auth/desktop/callback");
  expect(callback.searchParams.get("code")).toBe("a".repeat(43));
  expect(callback.searchParams.get("state")).toBe("expected-state");
  await listener.close();
  await listener.close();
});
