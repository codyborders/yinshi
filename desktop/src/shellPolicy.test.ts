import { expect, it } from "vitest";

import { createShellPolicy } from "./shellPolicy.js";

it("allows only the active helper origin and exact HTTPS external origins", () => {
  const policy = createShellPolicy({
    applicationOrigin: "http://127.0.0.1:43123",
    signInUrl: "file:///Applications/Yinshi.app/Contents/Resources/assets/signin.html",
    externalOrigins: ["https://yinshi.io", "https://docs.yinshi.io"],
  });

  expect(policy.navigationAllowed("http://127.0.0.1:43123/workspaces/one")).toBe(true);
  expect(policy.navigationAllowed("http://127.0.0.1:43124/workspaces/one")).toBe(false);
  expect(policy.navigationAllowed("https://yinshi.io/account")).toBe(false);
  expect(
    policy.navigationAllowed(
      "file:///Applications/Yinshi.app/Contents/Resources/assets/signin.html",
    ),
  ).toBe(true);
  expect(policy.externalAllowed("https://docs.yinshi.io/security")).toBe(true);
  expect(policy.externalAllowed("http://docs.yinshi.io/security")).toBe(false);
  expect(policy.externalAllowed("https://docs.yinshi.io.attacker.example/security")).toBe(false);
  expect(policy.externalAllowed("javascript:alert(1)")).toBe(false);
});
