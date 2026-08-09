import type { DesktopAccountSession } from "./accountSession.js";
import type { DesktopCredentialProfile } from "./credentialStore.js";

export interface HostedAccessSessionOptions {
  readonly resume: () => Promise<DesktopAccountSession>;
}

export class HostedAccessSession {
  readonly #resume: HostedAccessSessionOptions["resume"];
  #profile: DesktopCredentialProfile | undefined;
  #accessToken: string | undefined;
  #accessTokenExpiresAt: number | undefined;
  #refresh: Promise<DesktopAccountSession> | undefined;
  #epoch = 0;

  constructor(options: HostedAccessSessionOptions) {
    if (typeof options.resume !== "function") {
      throw new TypeError("HostedAccessSession requires a resume function");
    }
    this.#resume = options.resume;
  }

  get profile(): DesktopCredentialProfile | undefined {
    return this.#profile;
  }

  setAccount(account: DesktopAccountSession): void {
    if (account === null || typeof account !== "object") {
      throw new TypeError("Hosted account session must be an object");
    }
    this.#epoch += 1;
    this.#applyAccount(account);
  }

  clear(): void {
    this.#epoch += 1;
    this.#profile = undefined;
    this.#accessToken = undefined;
    this.#accessTokenExpiresAt = undefined;
    this.#refresh = undefined;
  }

  getCachedAccessToken(currentTimeSeconds: number): string | undefined {
    this.#requireCurrentTime(currentTimeSeconds);
    if (
      this.#accessToken === undefined ||
      this.#accessTokenExpiresAt === undefined ||
      this.#accessTokenExpiresAt <= currentTimeSeconds + 30
    ) {
      return undefined;
    }
    return this.#accessToken;
  }

  async getAccessToken(currentTimeSeconds: number): Promise<string> {
    const cachedToken = this.getCachedAccessToken(currentTimeSeconds);
    if (cachedToken !== undefined) {
      return cachedToken;
    }
    const account = await this.resumeAccount();
    if (account.mode !== "online") {
      throw new Error("Hosted account is offline");
    }
    return account.accessToken;
  }

  /**
   * Single entry point for spending the stored refresh token. The control
   * plane treats a replayed refresh token as device compromise and revokes the
   * device, so every caller shares one in-flight attempt.
   */
  async resumeAccount(): Promise<DesktopAccountSession> {
    const inFlight = this.#refresh;
    if (inFlight !== undefined) {
      return inFlight;
    }

    const refreshEpoch = this.#epoch;
    const refreshPromise = (async () => {
      const account = await this.#resume();
      if (this.#epoch !== refreshEpoch) {
        throw new Error("Hosted account changed while access was refreshing");
      }
      this.#applyAccount(account);
      return account;
    })();
    this.#refresh = refreshPromise;
    try {
      return await refreshPromise;
    } finally {
      if (this.#refresh === refreshPromise) {
        this.#refresh = undefined;
      }
    }
  }

  #applyAccount(account: DesktopAccountSession): void {
    this.#profile = account.mode === "signed-out" ? undefined : account.profile;
    this.#accessToken =
      account.mode === "online" ? account.accessToken : undefined;
    this.#accessTokenExpiresAt =
      account.mode === "online" ? account.accessTokenExpiresAt : undefined;
  }

  #requireCurrentTime(currentTimeSeconds: number): void {
    if (!Number.isSafeInteger(currentTimeSeconds) || currentTimeSeconds < 0) {
      throw new RangeError("currentTimeSeconds must be a non-negative integer");
    }
  }
}
