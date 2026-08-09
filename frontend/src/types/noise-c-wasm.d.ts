declare module "@richardhopton/noise-c.wasm" {
  export interface NoiseCipherState {
    EncryptWithAd(additionalData: Uint8Array, plaintext: Uint8Array): Uint8Array;
    DecryptWithAd(additionalData: Uint8Array, ciphertext: Uint8Array): Uint8Array;
    free(): void;
  }

  export interface NoiseHandshakeState {
    Initialize(
      prologue: Uint8Array | null,
      staticPrivateKey: Uint8Array | null,
      remoteStaticPublicKey: Uint8Array | null,
      presharedKey?: Uint8Array | null,
    ): void;
    WriteMessage(payload?: Uint8Array | null): Uint8Array;
    ReadMessage(
      message: Uint8Array,
      payloadNeeded?: boolean,
      fallbackSupported?: boolean,
    ): Uint8Array | null;
    GetHandshakeHash(): Uint8Array;
    Split(): [NoiseCipherState, NoiseCipherState];
    free(): void;
  }

  export interface NoiseLibrary {
    readonly constants: {
      readonly NOISE_ROLE_INITIATOR: number;
      readonly NOISE_DH_CURVE25519: number;
    };
    readonly HandshakeState: new (
      protocolName: string,
      role: number,
    ) => NoiseHandshakeState;
    CreateKeyPair(curveId: number): [Uint8Array, Uint8Array];
  }

  export interface NoiseModuleOptions {
    locateFile?: (path: string, prefix: string) => string;
  }

  type CreateNoise = (
    options: NoiseModuleOptions,
    callback: (library: NoiseLibrary) => void,
  ) => void;

  const createNoise: CreateNoise;
  export default createNoise;
}
