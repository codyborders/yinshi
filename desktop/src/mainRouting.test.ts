import { describe, expect, it } from "vitest";

import { applicationAppUrl } from "./mainRouting.js";

describe("applicationAppUrl", () => {
  it("loads authenticated application route when reopening a window", () => {
    expect(applicationAppUrl("https://yinshi.io")).toBe("https://yinshi.io/app");
  });
});
