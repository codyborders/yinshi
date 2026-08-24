import { describe, expect, it } from "vitest";

import indexHtml from "../index.html?raw";

describe("frontend content security policy", () => {
  it("blocks inline scripts while allowing required React style attributes", () => {
    const contentSecurityPolicy = indexHtml.match(
      /http-equiv="Content-Security-Policy"\s+content="([^"]+)"/,
    )?.[1];

    expect(contentSecurityPolicy).toBeDefined();
    expect(contentSecurityPolicy).toContain("script-src 'self'");
    expect(contentSecurityPolicy).toContain("worker-src 'self'");
    expect(contentSecurityPolicy).not.toContain(
      "script-src 'self' 'unsafe-inline'",
    );
    expect(contentSecurityPolicy).toContain(
      "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    );
  });
});
