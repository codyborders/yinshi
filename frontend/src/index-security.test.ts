import { describe, expect, it } from "vitest";

import indexHtml from "../index.html?raw";

describe("frontend index security", () => {
  it("blocks inline scripts while allowing required React style attributes", () => {
    const contentSecurityPolicy = indexHtml.match(
      /http-equiv="Content-Security-Policy"\s+content="([^"]+)"/,
    )?.[1];

    expect(contentSecurityPolicy).toBeDefined();
    expect(contentSecurityPolicy).toContain("script-src 'self'");
    // Replay compression uses a local Blob worker; inline scripts stay blocked.
    expect(contentSecurityPolicy).toContain("worker-src 'self' blob:;");
    expect(contentSecurityPolicy).not.toContain(
      "script-src 'self' 'unsafe-inline'",
    );
    expect(contentSecurityPolicy).toContain(
      "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    );
  });

  it("loads the main module without preloading Noise", () => {
    expect(indexHtml).toContain(
      '<script type="module" src="/src/main.tsx"></script>',
    );
    expect(indexHtml).not.toContain("/src/preload.ts");
    expect(indexHtml).not.toContain("noise-c.wasm");
    expect(indexHtml).not.toMatch(
      /<link\s[^>]*rel=["']preload["'][^>]*type=["']application\/wasm["'][^>]*>/i,
    );
  });
});
