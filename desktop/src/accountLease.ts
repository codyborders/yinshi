import { createPublicKey, timingSafeEqual, verify } from "node:crypto";

const LEASE_DURATION_SECONDS_MAX = 30 * 24 * 60 * 60;
const ED25519_SPKI_PREFIX = Buffer.from("302a300506032b6570032100", "hex");

export interface AccountLeaseClaims {
  readonly userId: string;
  readonly deviceId: string;
  readonly issuedAt: number;
  readonly expiresAt: number;
}

export interface VerifyAccountLeaseOptions {
  readonly token: string;
  readonly signingPublicKey: string;
  readonly currentTimeSeconds?: number;
}

function invalidLease(): never {
  throw new Error("account lease is invalid");
}

function decodeCanonicalBase64url(value: string): Buffer {
  if (typeof value !== "string" || value.length === 0 || !/^[A-Za-z0-9_-]+$/.test(value)) {
    return invalidLease();
  }
  const decoded = Buffer.from(value, "base64url");
  if (decoded.toString("base64url") !== value) {
    return invalidLease();
  }
  return decoded;
}

function parseJsonObject(encodedValue: string): Record<string, unknown> {
  let value: unknown;
  try {
    value = JSON.parse(decodeCanonicalBase64url(encodedValue).toString("utf8"));
  } catch {
    return invalidLease();
  }
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return invalidLease();
  }
  return value as Record<string, unknown>;
}

export function assertSigningKeyContinuity(
  pinnedSigningPublicKey: string | undefined,
  receivedSigningPublicKey: string,
): string {
  if (typeof receivedSigningPublicKey !== "string") {
    throw new TypeError("receivedSigningPublicKey must be a string");
  }
  let receivedKey: Buffer;
  try {
    receivedKey = decodeCanonicalBase64url(receivedSigningPublicKey);
  } catch {
    throw new Error("desktop signing key is invalid");
  }
  if (receivedKey.length !== 32) {
    throw new Error("desktop signing key is invalid");
  }
  if (pinnedSigningPublicKey === undefined) {
    return receivedSigningPublicKey;
  }
  if (typeof pinnedSigningPublicKey !== "string") {
    throw new TypeError("pinnedSigningPublicKey must be a string or undefined");
  }
  let pinnedKey: Buffer;
  try {
    pinnedKey = decodeCanonicalBase64url(pinnedSigningPublicKey);
  } catch {
    throw new Error("pinned desktop signing key is invalid");
  }
  if (pinnedKey.length !== 32) {
    throw new Error("pinned desktop signing key is invalid");
  }
  if (!timingSafeEqual(pinnedKey, receivedKey)) {
    throw new Error("desktop signing key changed");
  }
  return pinnedSigningPublicKey;
}


export function verifyAccountLease(options: VerifyAccountLeaseOptions): AccountLeaseClaims {
  if (typeof options.token !== "string" || typeof options.signingPublicKey !== "string") {
    return invalidLease();
  }
  const segments = options.token.split(".");
  if (segments.length !== 3) {
    return invalidLease();
  }
  const [encodedHeader, encodedPayload, encodedSignature] = segments;
  if (encodedHeader === undefined || encodedPayload === undefined || encodedSignature === undefined) {
    return invalidLease();
  }

  const header = parseJsonObject(encodedHeader);
  if (header.alg !== "EdDSA" || header.typ !== "YINSHI-LEASE" || header.v !== 1) {
    return invalidLease();
  }
  const payload = parseJsonObject(encodedPayload);
  const userId = payload.sub;
  const deviceId = payload.device_id;
  const issuedAt = payload.iat;
  const expiresAt = payload.exp;
  if (payload.aud !== "yinshi-desktop" || payload.v !== 1) {
    return invalidLease();
  }
  if (typeof userId !== "string" || userId.length === 0) {
    return invalidLease();
  }
  if (typeof deviceId !== "string" || deviceId.length === 0) {
    return invalidLease();
  }
  if (typeof issuedAt !== "number" || !Number.isSafeInteger(issuedAt)) {
    return invalidLease();
  }
  if (typeof expiresAt !== "number" || !Number.isSafeInteger(expiresAt)) {
    return invalidLease();
  }

  const currentTimeSeconds = options.currentTimeSeconds ?? Math.floor(Date.now() / 1_000);
  if (!Number.isSafeInteger(currentTimeSeconds) || currentTimeSeconds < 1) {
    throw new TypeError("currentTimeSeconds must be a positive integer");
  }
  if (issuedAt > currentTimeSeconds + 60 || expiresAt <= currentTimeSeconds) {
    return invalidLease();
  }
  if (expiresAt <= issuedAt || expiresAt - issuedAt > LEASE_DURATION_SECONDS_MAX) {
    return invalidLease();
  }

  const rawPublicKey = decodeCanonicalBase64url(options.signingPublicKey);
  const signature = decodeCanonicalBase64url(encodedSignature);
  if (rawPublicKey.length !== 32 || signature.length !== 64) {
    return invalidLease();
  }
  let validSignature = false;
  try {
    const publicKey = createPublicKey({
      key: Buffer.concat([ED25519_SPKI_PREFIX, rawPublicKey]),
      format: "der",
      type: "spki",
    });
    validSignature = verify(
      null,
      Buffer.from(`${encodedHeader}.${encodedPayload}`, "ascii"),
      publicKey,
      signature,
    );
  } catch {
    return invalidLease();
  }
  if (!validSignature) {
    return invalidLease();
  }
  return { userId, deviceId, issuedAt, expiresAt };
}
