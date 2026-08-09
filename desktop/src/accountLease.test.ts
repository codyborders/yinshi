import {
  generateKeyPairSync,
  sign,
  type KeyObject,
} from "node:crypto";

import { expect, it } from "vitest";

import {
  assertSigningKeyContinuity,
  verifyAccountLease,
} from "./accountLease.js";

function encodeJson(value: object): string {
  return Buffer.from(JSON.stringify(value), "utf8").toString("base64url");
}

function rawPublicKey(publicKey: KeyObject): string {
  const spki = publicKey.export({ format: "der", type: "spki" });
  return spki.subarray(spki.length - 32).toString("base64url");
}

it("accepts a current 30-day account lease signed by the pinned key", () => {
  const issuedAt = 1_700_000_000;
  const expiresAt = issuedAt + 30 * 24 * 60 * 60;
  const { privateKey, publicKey } = generateKeyPairSync("ed25519");
  const header = encodeJson({ alg: "EdDSA", typ: "YINSHI-LEASE", v: 1 });
  const payload = encodeJson({
    aud: "yinshi-desktop",
    sub: "user-id",
    device_id: "device-id",
    iat: issuedAt,
    exp: expiresAt,
    v: 1,
  });
  const signingInput = `${header}.${payload}`;
  const signature = sign(null, Buffer.from(signingInput, "ascii"), privateKey);
  const token = `${signingInput}.${signature.toString("base64url")}`;

  const claims = verifyAccountLease({
    token,
    signingPublicKey: rawPublicKey(publicKey),
    currentTimeSeconds: issuedAt + 1,
  });

  expect(claims).toEqual({
    userId: "user-id",
    deviceId: "device-id",
    issuedAt,
    expiresAt,
  });
  expect(() =>
    verifyAccountLease({
      token,
      signingPublicKey: rawPublicKey(publicKey),
      currentTimeSeconds: expiresAt,
    }),
  ).toThrow("account lease is invalid");

  const pinnedKey = rawPublicKey(publicKey);
  const replacement = generateKeyPairSync("ed25519");
  expect(assertSigningKeyContinuity(undefined, pinnedKey)).toBe(pinnedKey);
  expect(assertSigningKeyContinuity(pinnedKey, pinnedKey)).toBe(pinnedKey);
  expect(() =>
    assertSigningKeyContinuity(pinnedKey, rawPublicKey(replacement.publicKey)),
  ).toThrow("desktop signing key changed");
  expect(() =>
    verifyAccountLease({
      token,
      signingPublicKey: rawPublicKey(replacement.publicKey),
      currentTimeSeconds: issuedAt + 1,
    }),
  ).toThrow("account lease is invalid");

  const replacementTail = token.endsWith("A") ? "B" : "A";
  expect(() =>
    verifyAccountLease({
      token: `${token.slice(0, -1)}${replacementTail}`,
      signingPublicKey: pinnedKey,
      currentTimeSeconds: issuedAt + 1,
    }),
  ).toThrow("account lease is invalid");
});
