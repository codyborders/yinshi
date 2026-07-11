import path from "node:path";

import type { BrowserWindowConstructorOptions } from "electron";

export function createBrowserWindowOptions(
  preloadPath: string,
): BrowserWindowConstructorOptions {
  if (typeof preloadPath !== "string" || preloadPath.length === 0) {
    throw new TypeError("preloadPath must be a non-empty string");
  }
  if (!path.isAbsolute(preloadPath)) {
    throw new TypeError("preloadPath must be absolute");
  }

  return {
    show: false,
    webPreferences: {
      preload: preloadPath,
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      allowRunningInsecureContent: false,
    },
  };
}
