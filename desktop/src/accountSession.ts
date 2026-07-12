import { verifyAccountLease } from "./accountLease.js";
import type { DesktopCredentialProfile } from "./credentialStore.js";
import {
  readHostedDesktopTokenResponse,
  type HostedAuthCredentialStore,
} from "./hostedAuth.js";

export class DesktopSignInRequiredError extends Error {
  constructor() {
    super("desktop sign-in is required");
    this.name = "DesktopSignInRequiredError";
  }
}

export interface ResumeDesktopAccountOptions {
  readonly apiBaseUrl: string;
  readonly fetch: (
    input: string | URL,
    init?: RequestInit,
  ) => Promise<Response>;
  readonly credentialStore: HostedAuthCredentialStore;
  readonly currentTimeSeconds?: number;
}

export type DesktopAccountSession =
  | { readonly mode: "signed-out" }
  | { readonly mode: "offline"; readonly profile: DesktopCredentialProfile }
  | {
      readonly mode: "online";
      readonly accessToken: string;
      readonly accessTokenExpiresAt: number;
      readonly profile: DesktopCredentialProfile;
    };

function validateApiBaseUrl(value: string): URL {
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw new TypeError("apiBaseUrl must be a valid URL");
  }
  if (url.protocol !== "https:" || url.username !== "" || url.password !== "") {
    throw new TypeError("apiBaseUrl must be an HTTPS URL without credentials");
  }
  url.pathname = `${url.pathname.replace(/\/+$/, "")}/`;
  url.search = "";
  url.hash = "";
  return url;
}

function assertOfflineLease(
  profile: DesktopCredentialProfile,
  currentTimeSeconds: number,
): void {
  let claims;
  try {
    claims = verifyAccountLease({
      token: profile.accountLease,
      signingPublicKey: profile.signingPublicKey,
      currentTimeSeconds,
    });
  } catch {
    throw new DesktopSignInRequiredError();
  }
  if (
    claims.userId !== profile.user.id ||
    claims.deviceId !== profile.deviceId ||
    claims.expiresAt !== profile.accountLeaseExpiresAt
  ) {
    throw new DesktopSignInRequiredError();
  }
}

export async function resumeDesktopAccount(
  options: ResumeDesktopAccountOptions,
): Promise<DesktopAccountSession> {
  const apiBaseUrl = validateApiBaseUrl(options.apiBaseUrl);
  const currentTimeSeconds = (): number => {
    const value = options.currentTimeSeconds ?? Math.floor(Date.now() / 1_000);
    if (!Number.isSafeInteger(value) || value < 1) {
      throw new TypeError("currentTimeSeconds must be a positive integer");
    }
    return value;
  };
  currentTimeSeconds();
  const profile = await options.credentialStore.load();
  if (profile === null) {
    return { mode: "signed-out" };
  }

  let refreshResponse: Response;
  try {
    refreshResponse = await options.fetch(
      new URL("auth/desktop/refresh", apiBaseUrl),
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ refresh_token: profile.refreshToken }),
        redirect: "error",
        signal: AbortSignal.timeout(15_000),
      },
    );
  } catch {
    assertOfflineLease(profile, currentTimeSeconds());
    return { mode: "offline", profile };
  }
  if (refreshResponse.status >= 500 && refreshResponse.status <= 599) {
    assertOfflineLease(profile, currentTimeSeconds());
    return { mode: "offline", profile };
  }
  let hostedSession;
  try {
    hostedSession = await readHostedDesktopTokenResponse({
      response: refreshResponse,
      currentTimeSeconds: currentTimeSeconds(),
      pinnedSigningPublicKey: profile.signingPublicKey,
      expectedUserId: profile.user.id,
      expectedDeviceId: profile.deviceId,
    });
  } catch {
    throw new DesktopSignInRequiredError();
  }
  await options.credentialStore.save(hostedSession.profile);
  return { mode: "online", ...hostedSession };
}
