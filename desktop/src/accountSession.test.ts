import { generateKeyPairSync, sign, type KeyObject } from "node:crypto";

import { afterEach, expect, it, vi } from "vitest";

import type { DesktopCredentialProfile } from "./credentialStore.js";
import { resumeDesktopAccount } from "./accountSession.js";

function rawPublicKey(publicKey: KeyObject): string {
  const spki = publicKey.export({ format: "der", type: "spki" });
  return spki.subarray(spki.length - 32).toString("base64url");
}

function signLease(
  privateKey: KeyObject,
  issuedAt: number,
  expiresAt: number,
): string {
  const header = Buffer.from(
    JSON.stringify({ alg: "EdDSA", typ: "YINSHI-LEASE", v: 1 }),
  ).toString("base64url");
  const payload = Buffer.from(
    JSON.stringify({
      aud: "yinshi-desktop",
      sub: "user-id",
      device_id: "device-id",
      iat: issuedAt,
      exp: expiresAt,
      v: 1,
    }),
  ).toString("base64url");
  const signingInput = `${header}.${payload}`;
  return `${signingInput}.${sign(null, Buffer.from(signingInput, "ascii"), privateKey).toString("base64url")}`;
}

function createProfileFixture(issuedAt: number): {
  profile: DesktopCredentialProfile;
  privateKey: KeyObject;
} {
  const expiresAt = issuedAt + 30 * 24 * 60 * 60;
  const { privateKey, publicKey } = generateKeyPairSync("ed25519");
  return {
    privateKey,
    profile: {
      version: 1,
      refreshToken: "refresh-token",
      refreshTokenExpiresAt: issuedAt + 90 * 24 * 60 * 60,
      accountLease: signLease(privateKey, issuedAt, expiresAt),
      accountLeaseExpiresAt: expiresAt,
      signingPublicKey: rawPublicKey(publicKey),
      deviceId: "device-id",
      user: { id: "user-id", email: "user@example.com" },
    },
  };
}

function createProfile(issuedAt: number): DesktopCredentialProfile {
  return createProfileFixture(issuedAt).profile;
}

afterEach(() => {
  vi.useRealTimers();
});

it("restores local access from a valid account lease when hosted refresh is offline", async () => {
  const issuedAt = 1_700_000_000;
  const profile = createProfile(issuedAt);
  let saveCalled = false;

  const session = await resumeDesktopAccount({
    apiBaseUrl: "https://api.yinshi.io",
    currentTimeSeconds: issuedAt + 1,
    fetch: async () => {
      throw new TypeError("network unavailable");
    },
    credentialStore: {
      load: async () => profile,
      save: async () => {
        saveCalled = true;
      },
    },
  });

  expect(session).toEqual({ mode: "offline", profile });
  expect(saveCalled).toBe(false);
});

it("locks local access after the offline lease expires without deleting credentials", async () => {
  const issuedAt = 1_700_000_000;
  const profile = createProfile(issuedAt);
  let saveCalled = false;

  await expect(
    resumeDesktopAccount({
      apiBaseUrl: "https://api.yinshi.io",
      currentTimeSeconds: profile.accountLeaseExpiresAt,
      fetch: async () => {
        throw new TypeError("network unavailable");
      },
      credentialStore: {
        load: async () => profile,
        save: async () => {
          saveCalled = true;
        },
      },
    }),
  ).rejects.toThrow("desktop sign-in is required");
  expect(saveCalled).toBe(false);
});

it("rotates refresh credentials online while keeping access tokens in memory", async () => {
  const issuedAt = 1_700_000_000;
  const currentTimeSeconds = issuedAt + 10;
  const { profile, privateKey } = createProfileFixture(issuedAt);
  const leaseExpiresAt = currentTimeSeconds + 30 * 24 * 60 * 60;
  const rotatedLease = signLease(
    privateKey,
    currentTimeSeconds,
    leaseExpiresAt,
  );
  let savedProfile: DesktopCredentialProfile | undefined;
  let refreshRequest: Record<string, unknown> | undefined;

  const session = await resumeDesktopAccount({
    apiBaseUrl: "https://api.yinshi.io",
    currentTimeSeconds,
    fetch: async (_input, init) => {
      refreshRequest = JSON.parse(String(init?.body)) as Record<
        string,
        unknown
      >;
      return new Response(
        JSON.stringify({
          token_type: "Bearer",
          access_token: "rotated-access-token",
          access_token_expires_at: currentTimeSeconds + 15 * 60,
          refresh_token: "rotated-refresh-token",
          refresh_token_expires_at: currentTimeSeconds + 90 * 24 * 60 * 60,
          account_lease: rotatedLease,
          account_lease_expires_at: leaseExpiresAt,
          device_id: "device-id",
          signing_public_key: profile.signingPublicKey,
          user: { id: "user-id", email: "user@example.com" },
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    },
    credentialStore: {
      load: async () => profile,
      save: async (rotatedProfile) => {
        savedProfile = rotatedProfile;
      },
    },
  });

  expect(refreshRequest).toEqual({ refresh_token: "refresh-token" });
  expect(session.mode).toBe("online");
  if (session.mode !== "online") {
    throw new Error("expected online desktop account session");
  }
  expect(session.accessToken).toBe("rotated-access-token");
  expect(session.profile).toEqual(savedProfile);
  expect(savedProfile?.refreshToken).toBe("rotated-refresh-token");
  expect(savedProfile).not.toHaveProperty("accessToken");
});

it("validates rotated credentials against time after a delayed refresh", async () => {
  const issuedAt = 1_700_000_000;
  const responseIssuedAt = issuedAt + 2;
  const { profile, privateKey } = createProfileFixture(issuedAt);
  const leaseExpiresAt = responseIssuedAt + 30 * 24 * 60 * 60;
  const rotatedLease = signLease(privateKey, responseIssuedAt, leaseExpiresAt);
  vi.useFakeTimers();
  vi.setSystemTime(issuedAt * 1_000);

  const session = await resumeDesktopAccount({
    apiBaseUrl: "https://api.yinshi.io",
    fetch: async () => {
      vi.setSystemTime(responseIssuedAt * 1_000);
      return new Response(
        JSON.stringify({
          token_type: "Bearer",
          access_token: "rotated-access-token",
          access_token_expires_at: responseIssuedAt + 15 * 60,
          refresh_token: "rotated-refresh-token",
          refresh_token_expires_at: responseIssuedAt + 90 * 24 * 60 * 60,
          account_lease: rotatedLease,
          account_lease_expires_at: leaseExpiresAt,
          device_id: "device-id",
          signing_public_key: profile.signingPublicKey,
          user: { id: "user-id", email: "user@example.com" },
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    },
    credentialStore: {
      load: async () => profile,
      save: async () => undefined,
    },
  });

  expect(session.mode).toBe("online");
});

it("locks instead of falling back offline when hosted refresh rejects the device", async () => {
  const issuedAt = 1_700_000_000;
  const profile = createProfile(issuedAt);
  let saveCalled = false;

  await expect(
    resumeDesktopAccount({
      apiBaseUrl: "https://api.yinshi.io",
      currentTimeSeconds: issuedAt + 1,
      fetch: async () =>
        new Response(JSON.stringify({ detail: "invalid" }), {
          status: 401,
          headers: { "Content-Type": "application/json" },
        }),
      credentialStore: {
        load: async () => profile,
        save: async () => {
          saveCalled = true;
        },
      },
    }),
  ).rejects.toThrow("desktop sign-in is required");
  expect(saveCalled).toBe(false);
});
