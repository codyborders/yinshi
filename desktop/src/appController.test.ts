import { expect, it } from "vitest";

import { DesktopSignInRequiredError } from "./accountSession.js";
import type { DesktopCredentialProfile } from "./credentialStore.js";
import { DesktopAppController } from "./appController.js";

const profile: DesktopCredentialProfile = {
  version: 1,
  refreshToken: "refresh-token",
  refreshTokenExpiresAt: 1_800_000_000,
  accountLease: "account-lease",
  accountLeaseExpiresAt: 1_700_000_000,
  signingPublicKey: "signing-key",
  deviceId: "device-id",
  user: { id: "user-id", email: "user@example.com" },
};

it("gates helper startup on account state and tears it down on sign-out", async () => {
  const events: string[] = [];
  let stopped = false;
  const controller = new DesktopAppController({
    resumeAccount: async () => ({ mode: "signed-out" }),
    signIn: async () => ({
      accessToken: "memory-only-token",
      accessTokenExpiresAt: 1_700_000_900,
      profile,
    }),
    clearCredentials: async () => {
      events.push("credentials:clear");
    },
    startHelper: async (accountProfile) => {
      events.push(`helper:start:${accountProfile.user.id}`);
      return {
        ready: { port: 43123, instanceNonce: "a".repeat(43) },
        processId: 123,
        running: true,
        stop: async () => {
          stopped = true;
          events.push("helper:stop");
        },
      };
    },
    bootstrapHelper: async () => {
      events.push("helper:bootstrap");
      return "http://127.0.0.1:43123";
    },
    showSignIn: async () => {
      events.push("view:sign-in");
    },
    loadApplication: async (origin) => {
      events.push(`view:app:${origin}`);
    },
  });

  await controller.start();
  expect(events).toEqual(["view:sign-in"]);

  await controller.signIn();
  expect(events).toEqual([
    "view:sign-in",
    "helper:start:user-id",
    "helper:bootstrap",
    "view:app:http://127.0.0.1:43123",
  ]);

  await controller.signOut();
  expect(stopped).toBe(true);
  expect(events).toEqual([
    "view:sign-in",
    "helper:start:user-id",
    "helper:bootstrap",
    "view:app:http://127.0.0.1:43123",
    "helper:stop",
    "credentials:clear",
    "view:sign-in",
  ]);
});

it("shows sign-in without starting a helper when the offline lease is locked", async () => {
  let helperStarted = false;
  let signInShown = false;
  const controller = new DesktopAppController({
    resumeAccount: async () => {
      throw new DesktopSignInRequiredError();
    },
    signIn: async () => {
      throw new Error("not called");
    },
    clearCredentials: async () => undefined,
    startHelper: async () => {
      helperStarted = true;
      throw new Error("not called");
    },
    bootstrapHelper: async () => {
      throw new Error("not called");
    },
    showSignIn: async () => {
      signInShown = true;
    },
    loadApplication: async () => {
      throw new Error("not called");
    },
  });

  await controller.start();

  expect(signInShown).toBe(true);
  expect(helperStarted).toBe(false);
});
