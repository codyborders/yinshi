import { createHash, randomBytes } from "node:crypto";

import { assertSigningKeyContinuity, verifyAccountLease } from "./accountLease.js";
import type { DesktopCredentialProfile } from "./credentialStore.js";

const ACCESS_DURATION_SECONDS_MAX = 15 * 60;
const REFRESH_DURATION_SECONDS_MAX = 90 * 24 * 60 * 60;

export interface HostedAuthCredentialStore {
  load(): Promise<DesktopCredentialProfile | null>;
  save(profile: DesktopCredentialProfile): Promise<void>;
}

export interface StartHostedSignInOptions {
  readonly apiBaseUrl: string;
  readonly callbackUri: string;
  readonly deviceName: string;
  readonly fetch: (input: string | URL, init?: RequestInit) => Promise<Response>;
  readonly openExternal: (url: string) => Promise<void>;
  readonly waitForCallback: (expectedState: string) => Promise<URL>;
  readonly credentialStore: HostedAuthCredentialStore;
  readonly currentTimeSeconds?: number;
}

export interface HostedDesktopSession {
  readonly accessToken: string;
  readonly accessTokenExpiresAt: number;
  readonly profile: DesktopCredentialProfile;
}

function invalidResponse(): never {
  throw new Error("hosted desktop authentication response is invalid");
}

function requiredString(value: unknown): string {
  if (typeof value !== "string" || value.length === 0) {
    return invalidResponse();
  }
  return value;
}

function safeInteger(value: unknown): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value)) {
    return invalidResponse();
  }
  return value;
}

function parseApiBaseUrl(value: string): URL {
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw new TypeError("apiBaseUrl must be a valid URL");
  }
  if (
    url.protocol !== "https:" ||
    url.username !== "" ||
    url.password !== "" ||
    url.search !== "" ||
    url.hash !== ""
  ) {
    throw new TypeError("apiBaseUrl must be an HTTPS URL without credentials or query data");
  }
  url.pathname = `${url.pathname.replace(/\/+$/, "")}/`;
  return url;
}

function parseCallbackUri(value: string): URL {
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw new TypeError("callbackUri must be a valid URL");
  }
  if (
    url.protocol !== "http:" ||
    url.hostname !== "127.0.0.1" ||
    url.port === "" ||
    url.pathname !== "/auth/desktop/callback" ||
    url.username !== "" ||
    url.password !== "" ||
    url.search !== "" ||
    url.hash !== ""
  ) {
    throw new TypeError("callbackUri must be an exact loopback desktop callback URL");
  }
  return url;
}

async function readJsonObject(
  response: Response,
  expectedStatus: number,
): Promise<Record<string, unknown>> {
  if (!(response instanceof Response) || response.status !== expectedStatus) {
    return invalidResponse();
  }
  let value: unknown;
  try {
    value = await response.json();
  } catch {
    return invalidResponse();
  }
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return invalidResponse();
  }
  return value as Record<string, unknown>;
}

function validateAuthorizeUrl(value: unknown): string {
  const urlValue = requiredString(value);
  let url: URL;
  try {
    url = new URL(urlValue);
  } catch {
    return invalidResponse();
  }
  if (url.protocol !== "https:" || url.username !== "" || url.password !== "") {
    return invalidResponse();
  }
  return url.toString();
}

function validateCallback(callbackUrl: URL, expectedCallback: URL, expectedState: string): string {
  if (!(callbackUrl instanceof URL)) {
    throw new Error("desktop authorization callback is invalid");
  }
  if (callbackUrl.origin !== expectedCallback.origin || callbackUrl.pathname !== expectedCallback.pathname) {
    throw new Error("desktop authorization callback is invalid");
  }
  const states = callbackUrl.searchParams.getAll("state");
  const codes = callbackUrl.searchParams.getAll("code");
  if (states.length !== 1 || states[0] !== expectedState || codes.length !== 1) {
    throw new Error("desktop authorization callback is invalid");
  }
  const code = codes[0];
  if (code === undefined || code.length < 32 || code.length > 256) {
    throw new Error("desktop authorization callback is invalid");
  }
  return code;
}

export async function startHostedSignIn(
  options: StartHostedSignInOptions,
): Promise<HostedDesktopSession> {
  const apiBaseUrl = parseApiBaseUrl(options.apiBaseUrl);
  const callbackUrl = parseCallbackUri(options.callbackUri);
  if (typeof options.deviceName !== "string" || options.deviceName.trim().length === 0) {
    throw new TypeError("deviceName must be a non-empty string");
  }
  if (options.deviceName.trim().length > 100) {
    throw new TypeError("deviceName must not exceed 100 characters");
  }
  const currentTimeSeconds = options.currentTimeSeconds ?? Math.floor(Date.now() / 1_000);
  if (!Number.isSafeInteger(currentTimeSeconds) || currentTimeSeconds < 1) {
    throw new TypeError("currentTimeSeconds must be a positive integer");
  }

  const existingProfile = await options.credentialStore.load();
  const codeVerifier = randomBytes(64).toString("base64url");
  const codeChallenge = createHash("sha256").update(codeVerifier, "ascii").digest("base64url");
  const state = randomBytes(32).toString("base64url");
  const requestResponse = await options.fetch(
    new URL("auth/desktop/requests", apiBaseUrl),
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        redirect_uri: callbackUrl.toString(),
        code_challenge: codeChallenge,
        state,
      }),
      redirect: "error",
      signal: AbortSignal.timeout(15_000),
    },
  );
  const requestBody = await readJsonObject(requestResponse, 201);
  requiredString(requestBody.request_id);
  const authorizeUrl = validateAuthorizeUrl(requestBody.authorize_url);
  const authorizationExpiresAt = Date.parse(requiredString(requestBody.expires_at)) / 1_000;
  if (!Number.isSafeInteger(authorizationExpiresAt) || authorizationExpiresAt <= currentTimeSeconds) {
    return invalidResponse();
  }

  await options.openExternal(authorizeUrl);
  const callback = await options.waitForCallback(state);
  const authorizationCode = validateCallback(callback, callbackUrl, state);
  const tokenResponse = await options.fetch(new URL("auth/desktop/token", apiBaseUrl), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      authorization_code: authorizationCode,
      code_verifier: codeVerifier,
      device_name: options.deviceName.trim(),
    }),
    redirect: "error",
    signal: AbortSignal.timeout(15_000),
  });
  const tokenBody = await readJsonObject(tokenResponse, 200);
  if (tokenBody.token_type !== "Bearer") {
    return invalidResponse();
  }
  const accessToken = requiredString(tokenBody.access_token);
  const accessTokenExpiresAt = safeInteger(tokenBody.access_token_expires_at);
  const refreshToken = requiredString(tokenBody.refresh_token);
  const refreshTokenExpiresAt = safeInteger(tokenBody.refresh_token_expires_at);
  const accountLease = requiredString(tokenBody.account_lease);
  const accountLeaseExpiresAt = safeInteger(tokenBody.account_lease_expires_at);
  const deviceId = requiredString(tokenBody.device_id);
  const receivedSigningPublicKey = requiredString(tokenBody.signing_public_key);
  const signingPublicKey = assertSigningKeyContinuity(
    existingProfile?.signingPublicKey,
    receivedSigningPublicKey,
  );
  const userValue = tokenBody.user;
  if (typeof userValue !== "object" || userValue === null || Array.isArray(userValue)) {
    return invalidResponse();
  }
  const user = userValue as Record<string, unknown>;
  const userId = requiredString(user.id);
  const userEmail = requiredString(user.email);
  if (
    accessTokenExpiresAt <= currentTimeSeconds ||
    accessTokenExpiresAt - currentTimeSeconds > ACCESS_DURATION_SECONDS_MAX
  ) {
    return invalidResponse();
  }
  if (
    refreshTokenExpiresAt <= currentTimeSeconds ||
    refreshTokenExpiresAt - currentTimeSeconds > REFRESH_DURATION_SECONDS_MAX
  ) {
    return invalidResponse();
  }

  const leaseClaims = verifyAccountLease({
    token: accountLease,
    signingPublicKey,
    currentTimeSeconds,
  });
  if (
    leaseClaims.expiresAt !== accountLeaseExpiresAt ||
    leaseClaims.userId !== userId ||
    leaseClaims.deviceId !== deviceId
  ) {
    return invalidResponse();
  }
  const profile: DesktopCredentialProfile = {
    version: 1,
    refreshToken,
    refreshTokenExpiresAt,
    accountLease,
    accountLeaseExpiresAt,
    signingPublicKey,
    deviceId,
    user: { id: userId, email: userEmail },
  };
  await options.credentialStore.save(profile);
  return { accessToken, accessTokenExpiresAt, profile };
}
