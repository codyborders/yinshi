import noiseWasmUrl from "@richardhopton/noise-c.wasm/src/noise-c.wasm?url";
import type {
  NoiseCipherState,
  NoiseHandshakeState,
  NoiseLibrary,
} from "@richardhopton/noise-c.wasm";

const NOISE_PROTOCOL_NAME = "Noise_IK_25519_ChaChaPoly_SHA256";
const X25519_KEY_LENGTH = 32;
const NOISE_TAG_LENGTH = 16;
const NOISE_MESSAGE_MAX_LENGTH = 65_535;
const IK_FIRST_MESSAGE_OVERHEAD = 96;
const IK_SECOND_MESSAGE_MIN_LENGTH = 48;
const TRANSPORT_PLAINTEXT_MAX_LENGTH = NOISE_MESSAGE_MAX_LENGTH - NOISE_TAG_LENGTH;
const TRANSPORT_MESSAGES_BEFORE_REHANDSHAKE = 1_048_576;
const WASM_LOAD_TIMEOUT_MS = 15_000;
const emptyBytes = new Uint8Array();

let libraryPromise: Promise<NoiseLibrary> | undefined;

export interface NoiseIkKeypair {
  readonly privateKey: Uint8Array;
  readonly publicKey: Uint8Array;
}

export interface NoiseIkInitiatorOptions {
  readonly staticPrivateKey: Uint8Array;
  readonly responderStaticPublicKey: Uint8Array;
  readonly prologue: Uint8Array;
}

export interface NoiseIkInitiator {
  readonly handshakeHash: Uint8Array;
  writeHandshakeMessage(payload: Uint8Array): Uint8Array;
  readHandshakeMessage(message: Uint8Array): Uint8Array;
  encrypt(plaintext: Uint8Array): Uint8Array;
  decrypt(ciphertext: Uint8Array): Uint8Array;
  dispose(): void;
}

function copyExact(value: Uint8Array, length: number, name: string): Uint8Array {
  if (!(value instanceof Uint8Array) || value.length !== length) {
    throw new TypeError(`${name} must contain exactly ${length} bytes`);
  }
  return Uint8Array.from(value);
}

function copyBytes(value: Uint8Array, name: string): Uint8Array {
  if (!(value instanceof Uint8Array)) {
    throw new TypeError(`${name} must be Uint8Array`);
  }
  return Uint8Array.from(value);
}

async function loadNoiseLibrary(): Promise<NoiseLibrary> {
  if (libraryPromise !== undefined) {
    return libraryPromise;
  }
  libraryPromise = import("@richardhopton/noise-c.wasm").then(
    ({ default: createNoise }) =>
      new Promise<NoiseLibrary>((resolve, reject) => {
        const timeout = globalThis.setTimeout(() => {
          reject(new Error("Noise cryptography module did not load in time"));
        }, WASM_LOAD_TIMEOUT_MS);
        try {
          const processValue = (
            globalThis as typeof globalThis & {
              process?: { versions?: { node?: string } };
            }
          ).process;
          const moduleOptions =
            typeof processValue?.versions?.node === "string"
              ? {}
              : {
                  locateFile: (path: string) => {
                    if (path !== "noise-c.wasm") {
                      throw new Error("Noise cryptography requested an unknown runtime asset");
                    }
                    return noiseWasmUrl;
                  },
                };
          createNoise(
            moduleOptions,
            (library) => {
            globalThis.clearTimeout(timeout);
              resolve(library);
            },
          );
        } catch (error) {
          globalThis.clearTimeout(timeout);
          reject(error);
        }
      }),
  );
  return libraryPromise;
}

class NoiseIkInitiatorState implements NoiseIkInitiator {
  readonly #handshake: NoiseHandshakeState;
  #handshakeHashValue: Uint8Array | undefined;
  #sendCipher: NoiseCipherState | undefined;
  #receiveCipher: NoiseCipherState | undefined;
  #firstMessageWritten = false;
  #failed = false;
  #disposed = false;
  #messagesSent = 0;
  #messagesReceived = 0;

  constructor(library: NoiseLibrary, options: NoiseIkInitiatorOptions) {
    const staticPrivateKey = copyExact(
      options.staticPrivateKey,
      X25519_KEY_LENGTH,
      "staticPrivateKey",
    );
    const responderStaticPublicKey = copyExact(
      options.responderStaticPublicKey,
      X25519_KEY_LENGTH,
      "responderStaticPublicKey",
    );
    const prologue = copyBytes(options.prologue, "prologue");
    if (prologue.length > NOISE_MESSAGE_MAX_LENGTH) {
      throw new RangeError("Noise IK prologue is too large");
    }

    const handshake = new library.HandshakeState(
      NOISE_PROTOCOL_NAME,
      library.constants.NOISE_ROLE_INITIATOR,
    );
    try {
      handshake.Initialize(prologue, staticPrivateKey, responderStaticPublicKey);
    } catch (error) {
      handshake.free();
      throw error;
    } finally {
      staticPrivateKey.fill(0);
    }
    this.#handshake = handshake;
  }

  get handshakeHash(): Uint8Array {
    this.#requireUsable();
    if (this.#handshakeHashValue === undefined) {
      throw new Error("Noise IK handshake is not complete");
    }
    return Uint8Array.from(this.#handshakeHashValue);
  }

  writeHandshakeMessage(payloadValue: Uint8Array): Uint8Array {
    this.#requireUsable();
    if (this.#firstMessageWritten) {
      throw new Error("Noise IK initiator handshake message was already written");
    }
    const payload = copyBytes(payloadValue, "payload");
    if (payload.length > NOISE_MESSAGE_MAX_LENGTH - IK_FIRST_MESSAGE_OVERHEAD) {
      throw new RangeError("Noise IK initiator payload is too large");
    }
    try {
      const message = this.#handshake.WriteMessage(payload);
      if (message.length < IK_FIRST_MESSAGE_OVERHEAD || message.length > NOISE_MESSAGE_MAX_LENGTH) {
        throw new Error("Noise IK library produced an invalid initiator message");
      }
      this.#firstMessageWritten = true;
      return Uint8Array.from(message);
    } catch (error) {
      this.#failed = true;
      throw error;
    }
  }

  readHandshakeMessage(messageValue: Uint8Array): Uint8Array {
    this.#requireUsable();
    if (!this.#firstMessageWritten || this.#handshakeHashValue !== undefined) {
      throw new Error("Noise IK initiator is not ready for the responder message");
    }
    const message = copyBytes(messageValue, "message");
    if (message.length < IK_SECOND_MESSAGE_MIN_LENGTH || message.length > NOISE_MESSAGE_MAX_LENGTH) {
      throw new RangeError("Noise IK responder message has an invalid length");
    }
    try {
      const payload = this.#handshake.ReadMessage(message, true);
      if (payload === null) {
        throw new Error("Noise IK responder payload was not returned");
      }
      this.#handshakeHashValue = Uint8Array.from(this.#handshake.GetHandshakeHash());
      [this.#sendCipher, this.#receiveCipher] = this.#handshake.Split();
      return Uint8Array.from(payload);
    } catch (error) {
      this.#failed = true;
      throw error;
    }
  }

  encrypt(plaintextValue: Uint8Array): Uint8Array {
    const cipher = this.#transportCipher("send");
    const plaintext = copyBytes(plaintextValue, "plaintext");
    if (plaintext.length > TRANSPORT_PLAINTEXT_MAX_LENGTH) {
      throw new RangeError("Noise transport plaintext is too large");
    }
    if (this.#messagesSent >= TRANSPORT_MESSAGES_BEFORE_REHANDSHAKE) {
      throw new Error("Noise transport requires a fresh handshake");
    }
    try {
      const ciphertext = cipher.EncryptWithAd(emptyBytes, plaintext);
      this.#messagesSent += 1;
      return Uint8Array.from(ciphertext);
    } catch (error) {
      this.#failed = true;
      throw error;
    }
  }

  decrypt(ciphertextValue: Uint8Array): Uint8Array {
    const cipher = this.#transportCipher("receive");
    const ciphertext = copyBytes(ciphertextValue, "ciphertext");
    if (ciphertext.length < NOISE_TAG_LENGTH || ciphertext.length > NOISE_MESSAGE_MAX_LENGTH) {
      throw new RangeError("Noise transport ciphertext has an invalid length");
    }
    if (this.#messagesReceived >= TRANSPORT_MESSAGES_BEFORE_REHANDSHAKE) {
      throw new Error("Noise transport requires a fresh handshake");
    }
    try {
      const plaintext = cipher.DecryptWithAd(emptyBytes, ciphertext);
      this.#messagesReceived += 1;
      return Uint8Array.from(plaintext);
    } catch (error) {
      this.#failed = true;
      throw error;
    }
  }

  dispose(): void {
    if (this.#disposed) {
      return;
    }
    this.#disposed = true;
    if (this.#sendCipher !== undefined) {
      this.#sendCipher.free();
    }
    if (this.#receiveCipher !== undefined) {
      this.#receiveCipher.free();
    }
    if (this.#handshakeHashValue === undefined && !this.#failed) {
      this.#handshake.free();
    }
  }

  #transportCipher(direction: "send" | "receive"): NoiseCipherState {
    this.#requireUsable();
    const cipher = direction === "send" ? this.#sendCipher : this.#receiveCipher;
    if (cipher === undefined) {
      throw new Error("Noise IK transport is not ready");
    }
    return cipher;
  }

  #requireUsable(): void {
    if (this.#disposed) {
      throw new Error("Noise IK transport was disposed");
    }
    if (this.#failed) {
      throw new Error("Noise IK transport failed and cannot be reused");
    }
  }
}

export async function createNoiseIkKeypair(): Promise<NoiseIkKeypair> {
  const library = await loadNoiseLibrary();
  const [privateKey, publicKey] = library.CreateKeyPair(
    library.constants.NOISE_DH_CURVE25519,
  );
  return {
    privateKey: copyExact(privateKey, X25519_KEY_LENGTH, "generated private key"),
    publicKey: copyExact(publicKey, X25519_KEY_LENGTH, "generated public key"),
  };
}

export async function createNoiseIkInitiator(
  options: NoiseIkInitiatorOptions,
): Promise<NoiseIkInitiator> {
  const library = await loadNoiseLibrary();
  return new NoiseIkInitiatorState(library, options);
}
