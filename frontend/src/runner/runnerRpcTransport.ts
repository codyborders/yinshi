export const RPC_REQUEST_BYTES_MAX = 2 * 1_024 * 1_024;
export const RPC_RESPONSE_BYTES_MAX = 10 * 1_024 * 1_024;
export const NOISE_CIPHERTEXT_BYTES_MAX = 65_535;
export const NOISE_TAG_BYTES = 16;
export const NOISE_PLAINTEXT_BYTES_MAX =
  NOISE_CIPHERTEXT_BYTES_MAX - NOISE_TAG_BYTES;
export const TRANSPORT_HEADER_BYTES = 17;
export const TRANSPORT_PAYLOAD_BYTES_MAX =
  NOISE_CIPHERTEXT_BYTES_MAX - NOISE_TAG_BYTES - TRANSPORT_HEADER_BYTES;
export const TRANSPORT_REQUEST = 1;
export const TRANSPORT_ACK = 2;
export const TRANSPORT_RESPONSE = 3;
export const TRANSPORT_PULL = 4;
export const TRANSPORT_MAGIC = Uint8Array.of(89, 82, 80, 49);

export interface TransportFragment {
  readonly kind: number;
  readonly index: number;
  readonly count: number;
  readonly total: number;
  readonly payload: Uint8Array;
}

export function fragmentCount(total: number): number {
  if (!Number.isSafeInteger(total) || total < 1) {
    throw new RangeError("Runner RPC transport total must be positive");
  }
  return Math.max(1, Math.ceil(total / TRANSPORT_PAYLOAD_BYTES_MAX));
}

export function packTransportFragment(
  kind: number,
  index: number,
  count: number,
  total: number,
  payload?: Uint8Array,
): Uint8Array {
  const payloadLength = payload?.length ?? 0;
  const frame = new Uint8Array(TRANSPORT_HEADER_BYTES + payloadLength);
  frame.set(TRANSPORT_MAGIC, 0);
  const view = new DataView(frame.buffer);
  view.setUint8(4, kind);
  view.setUint32(5, index);
  view.setUint32(9, count);
  view.setUint32(13, total);
  if (payload !== undefined) frame.set(payload, TRANSPORT_HEADER_BYTES);
  return frame;
}

export function parseTransportFragment(
  frame: Uint8Array,
  totalLimit: number,
): TransportFragment {
  if (frame.length < TRANSPORT_HEADER_BYTES) {
    throw new Error("Runner RPC transport fragment is invalid");
  }
  for (let index = 0; index < TRANSPORT_MAGIC.length; index += 1) {
    if (frame[index] !== TRANSPORT_MAGIC[index]) {
      throw new Error("Runner RPC transport fragment is invalid");
    }
  }
  const view = new DataView(frame.buffer, frame.byteOffset, frame.byteLength);
  const kind = view.getUint8(4);
  const index = view.getUint32(5);
  const count = view.getUint32(9);
  const total = view.getUint32(13);
  if (
    total < 1 ||
    total > totalLimit ||
    count !== fragmentCount(total) ||
    index >= count
  ) {
    throw new Error("Runner RPC transport fragment is invalid");
  }
  const expectedPayloadLength = Math.min(
    TRANSPORT_PAYLOAD_BYTES_MAX,
    total - index * TRANSPORT_PAYLOAD_BYTES_MAX,
  );
  if (frame.length !== TRANSPORT_HEADER_BYTES + expectedPayloadLength) {
    throw new Error("Runner RPC transport fragment is invalid");
  }
  return {
    kind,
    index,
    count,
    total,
    payload: frame.subarray(TRANSPORT_HEADER_BYTES),
  };
}

export function parseTransportAck(
  frame: Uint8Array,
  expectedIndex: number,
  expectedCount: number,
  expectedTotal: number,
): void {
  if (frame.length !== TRANSPORT_HEADER_BYTES) {
    throw new Error("Runner RPC transport fragment is invalid");
  }
  const view = new DataView(frame.buffer, frame.byteOffset, frame.byteLength);
  for (let index = 0; index < TRANSPORT_MAGIC.length; index += 1) {
    if (frame[index] !== TRANSPORT_MAGIC[index]) {
      throw new Error("Runner RPC transport fragment is invalid");
    }
  }
  if (
    view.getUint8(4) !== TRANSPORT_ACK ||
    view.getUint32(5) !== expectedIndex ||
    view.getUint32(9) !== expectedCount ||
    view.getUint32(13) !== expectedTotal
  ) {
    throw new Error("Runner RPC transport fragment is invalid");
  }
}

export function hasTransportMagic(value: Uint8Array): boolean {
  return (
    value.length >= TRANSPORT_MAGIC.length &&
    TRANSPORT_MAGIC.every((byte, index) => value[index] === byte)
  );
}
