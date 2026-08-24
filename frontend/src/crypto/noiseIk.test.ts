import { afterAll, describe, expect, it } from "vitest";

import {
  createNoiseIkInitiator,
  createNoiseIkKeypair,
  preloadNoiseIkLibrary,
  type NoiseIkInitiator,
} from "./noiseIk";

function bytes(hex: string): Uint8Array {
  return Uint8Array.from(Buffer.from(hex, "hex"));
}

function hex(value: Uint8Array): string {
  return Buffer.from(value).toString("hex");
}

const originalCryptoDescriptor = Object.getOwnPropertyDescriptor(
  globalThis,
  "crypto",
);

afterAll(() => {
  if (originalCryptoDescriptor === undefined) {
    Reflect.deleteProperty(globalThis, "crypto");
  } else {
    Object.defineProperty(globalThis, "crypto", originalCryptoDescriptor);
  }
});

describe("Noise_IK_25519_ChaChaPoly_SHA256", () => {
  it("shares one cached preload with later key creation", async () => {
    const first = preloadNoiseIkLibrary();
    const second = preloadNoiseIkLibrary();

    expect(first).toBe(second);
    await first;
    const keypair = await createNoiseIkKeypair();
    expect(keypair.privateKey).toHaveLength(32);
    expect(keypair.publicKey).toHaveLength(32);
  });

  it("matches the canonical IK handshake and transport vector", async () => {
    const ephemeralPrivateKey = bytes(
      "893e28b9dc6ca8d611ab664754b8ceb7bac5117349a4439a6b0569da977c464a",
    );
    Object.defineProperty(globalThis, "crypto", {
      configurable: true,
      value: {
        getRandomValues(target: Uint8Array): Uint8Array {
          expect(target.length).toBe(ephemeralPrivateKey.length);
          target.set(ephemeralPrivateKey);
          return target;
        },
      },
    });

    let initiator: NoiseIkInitiator | undefined;
    try {
      initiator = await createNoiseIkInitiator({
        staticPrivateKey: bytes(
          "e61ef9919cde45dd5f82166404bd08e38bceb5dfdfded0a34c8df7ed542214d1",
        ),
        responderStaticPublicKey: bytes(
          "31e0303fd6418d2f8c0e78b91f22e8caed0fbe48656dcf4767e4834f701b8f62",
        ),
        prologue: bytes("4a6f686e2047616c74"),
      });

      const firstMessage = initiator.writeHandshakeMessage(
        bytes("4c756477696720766f6e204d69736573"),
      );
      expect(hex(firstMessage)).toBe(
        "ca35def5ae56cec33dc2036731ab14896bc4c75dbb07a61f879f8e3afa4c7944" +
          "718da798efbcd91528520204f904b9bd6c7413dccdc214d951e15253e39987f" +
          "18146e8cd0873654207148333479d4d16c289f0294b29960a72f48e0b7bba2" +
          "e89083169825e59642148d492020664ccf7",
      );

      const responsePayload = initiator.readHandshakeMessage(
        bytes(
          "95ebc60d2b1fa672c1f46a8aa265ef51bfe38e7ccb39ec5be34069f144808843" +
            "5361e70b2ed446e6c9ec387d1d6b3b840f194e373979d241b203c4acafccf5",
        ),
      );
      expect(hex(responsePayload)).toBe("4d757272617920526f746862617264");
      expect(hex(initiator.handshakeHash)).toBe(
        "0b0f68fb0c27e03ce9b97565995ed4838cc0581b762ef72b062f6a546419fad7",
      );
      expect(hex(initiator.encrypt(bytes("462e20412e20486179656b")))).toBe(
        "050e9f3c8fac16b68dbce8f8c4bfbf6617c897f9ada4aa29aa19c8",
      );
      expect(
        hex(
          initiator.decrypt(
            bytes("344233a6cabb7141d80f3da2fedc311d9646bbb0f505afe403a667"),
          ),
        ),
      ).toBe("4361726c204d656e676572");
    } finally {
      initiator?.dispose();
    }
  });
});
