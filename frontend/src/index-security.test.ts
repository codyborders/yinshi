import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

describe("frontend content security policy", () => {
  it("blocks inline scripts while allowing required React style attributes", () => {
    const indexHtml = readFileSync(resolve(process.cwd(), "index.html"), "utf-8");
    const contentSecurityPolicy = indexHtml.match(
      /http-equiv="Content-Security-Policy"\s+content="([^"]+)"/,
    )?.[1];

    expect(contentSecurityPolicy).toBeDefined();
    expect(contentSecurityPolicy).toContain("script-src 'self'");
    expect(contentSecurityPolicy).not.toContain("script-src 'self' 'unsafe-inline'");
    expect(contentSecurityPolicy).toContain(
      "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    );
  });
});
