// Covers memory-only hosted access refresh, concurrency, and logout races.

import { describe, expect, it, vi } from "vitest";

import { HostedAccessSession } from "./hostedAccessSession.js";
import type { DesktopAccountSession } from "./accountSession.js";

const profile = {
  version: 1 as const,
  refreshToken: "refresh-token",
  refreshTokenExpiresAt: 2_000,
  accountLease: "lease-token",
  accountLeaseExpiresAt: 1_900,
  signingPublicKey: "signing-key",
  deviceId: "device-1",
  user: { id: "user-1", email: "owner@example.com" },
};

function online(accessToken: string): DesktopAccountSession {
  return {
    mode: "online",
    profile,
    accessToken,
    accessTokenExpiresAt: 1_500,
  };
}

describe("HostedAccessSession", () => {
  it("coalesces concurrent refreshes and keeps access tokens in memory", async () => {
    const resume = vi.fn(async () => online("a".repeat(40)));
    const session = new HostedAccessSession({ resume });

    const [first, second] = await Promise.all([
      session.getAccessToken(1_000),
      session.getAccessToken(1_000),
    ]);

    expect(first).toBe("a".repeat(40));
    expect(second).toBe(first);
    expect(resume).toHaveBeenCalledOnce();
    expect(resume).toHaveBeenCalledWith();
    expect(session.profile).toEqual(profile);
  });

  it("cannot restore credentials when logout wins an in-flight refresh", async () => {
    let resolveResume: ((account: DesktopAccountSession) => void) | undefined;
    const resume = vi.fn(
      () =>
        new Promise<DesktopAccountSession>((resolve) => {
          resolveResume = resolve;
        }),
    );
    const session = new HostedAccessSession({ resume });
    const refresh = session.getAccessToken(1_000);

    session.clear();
    resolveResume?.(online("b".repeat(40)));

    await expect(refresh).rejects.toThrow("account changed");
    expect(session.profile).toBeUndefined();
    expect(session.getCachedAccessToken(1_000)).toBeUndefined();
  });
});
