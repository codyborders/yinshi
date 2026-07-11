import { createHash, generateKeyPairSync, sign, type KeyObject } from "node:crypto";

import { expect, it } from "vitest";

import type { DesktopCredentialProfile } from "./credentialStore.js";
import { startHostedSignIn } from "./hostedAuth.js";

function rawPublicKey(publicKey: KeyObject): string {
  const spki = publicKey.export({ format: "der", type: "spki" });
  return spki.subarray(spki.length - 32).toString("base64url");
}

function createLease(issuedAt: number, expiresAt: number): {
  token: string;
  signingPublicKey: string;
} {
  const { privateKey, publicKey } = generateKeyPairSync("ed25519");
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
  const signature = sign(null, Buffer.from(signingInput, "ascii"), privateKey);
  return {
    token: `${signingInput}.${signature.toString("base64url")}`,
    signingPublicKey: rawPublicKey(publicKey),
  };
}

it("completes hosted PKCE sign-in and persists only refresh and lease credentials", async () => {
  const issuedAt = 1_700_000_000;
  const lease = createLease(issuedAt, issuedAt + 30 * 24 * 60 * 60);
  const requests: Array<{ url: string; body: Record<string, unknown> }> = [];
  let openedUrl: string | undefined;
  let savedProfile: DesktopCredentialProfile | undefined;

  const session = await startHostedSignIn({
    apiBaseUrl: "https://api.yinshi.io",
    callbackUri: "http://127.0.0.1:43123/auth/desktop/callback",
    deviceName: "Test Mac",
    currentTimeSeconds: issuedAt,
    fetch: async (input, init) => {
      const url = input.toString();
      const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
      requests.push({ url, body });
      if (url.endsWith("/auth/desktop/requests")) {
        return new Response(
          JSON.stringify({
            request_id: "request-id",
            authorize_url: "https://app.yinshi.io/auth/desktop/authorize/request-id",
            expires_at: "2023-11-14T22:18:20Z",
          }),
          { status: 201, headers: { "Content-Type": "application/json" } },
        );
      }
      return new Response(
        JSON.stringify({
          token_type: "Bearer",
          access_token: "memory-only-access-token",
          access_token_expires_at: issuedAt + 15 * 60,
          refresh_token: "keychain-refresh-token",
          refresh_token_expires_at: issuedAt + 90 * 24 * 60 * 60,
          account_lease: lease.token,
          account_lease_expires_at: issuedAt + 30 * 24 * 60 * 60,
          device_id: "device-id",
          signing_public_key: lease.signingPublicKey,
          user: { id: "user-id", email: "user@example.com" },
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    },
    openExternal: async (url) => {
      openedUrl = url;
    },
    waitForCallback: async (expectedState) =>
      new URL(
        `http://127.0.0.1:43123/auth/desktop/callback?code=${"a".repeat(43)}&state=${expectedState}`,
      ),
    credentialStore: {
      load: async () => null,
      save: async (profile) => {
        savedProfile = profile;
      },
    },
  });

  expect(openedUrl).toBe("https://app.yinshi.io/auth/desktop/authorize/request-id");
  expect(requests.map((request) => request.url)).toEqual([
    "https://api.yinshi.io/auth/desktop/requests",
    "https://api.yinshi.io/auth/desktop/token",
  ]);
  const verifier = String(requests[1]?.body.code_verifier);
  const challenge = createHash("sha256").update(verifier, "ascii").digest("base64url");
  expect(requests[0]?.body.code_challenge).toBe(challenge);
  expect(requests[1]?.body.authorization_code).toBe("a".repeat(43));
  expect(session.accessToken).toBe("memory-only-access-token");
  expect(savedProfile).toEqual(session.profile);
  expect(savedProfile).not.toHaveProperty("accessToken");
  expect(savedProfile?.refreshToken).toBe("keychain-refresh-token");
});

it("rejects a hosted callback whose state does not match the PKCE request", async () => {
  let saveCalled = false;
  await expect(
    startHostedSignIn({
      apiBaseUrl: "https://api.yinshi.io",
      callbackUri: "http://127.0.0.1:43123/auth/desktop/callback",
      deviceName: "Test Mac",
      currentTimeSeconds: 1_700_000_000,
      fetch: async () =>
        new Response(
          JSON.stringify({
            request_id: "request-id",
            authorize_url: "https://app.yinshi.io/auth/desktop/authorize/request-id",
            expires_at: "2023-11-14T22:18:20Z",
          }),
          { status: 201, headers: { "Content-Type": "application/json" } },
        ),
      openExternal: async () => undefined,
      waitForCallback: async () =>
        new URL(
          `http://127.0.0.1:43123/auth/desktop/callback?code=${"a".repeat(43)}&state=wrong-state`,
        ),
      credentialStore: {
        load: async () => null,
        save: async () => {
          saveCalled = true;
        },
      },
    }),
  ).rejects.toThrow("desktop authorization callback is invalid");
  expect(saveCalled).toBe(false);
});
