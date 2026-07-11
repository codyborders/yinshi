// Covers Electron renderer isolation by asserting the complete BrowserWindow security policy.

import path from "node:path";

import { describe, expect, it } from "vitest";

import { createBrowserWindowOptions } from "./security.js";

describe("createBrowserWindowOptions", () => {
  it("isolates the renderer behind an absolute preload path", () => {
    const preloadPath = path.resolve("/tmp/yinshi/preload.js");

    const options = createBrowserWindowOptions(preloadPath);

    expect(options.show).toBe(false);
    expect(options.webPreferences).toEqual(
      expect.objectContaining({
        preload: preloadPath,
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true,
        webSecurity: true,
        allowRunningInsecureContent: false,
      }),
    );
    expect(() => createBrowserWindowOptions("relative/preload.js")).toThrow(
      "preloadPath must be absolute",
    );
  });
});
