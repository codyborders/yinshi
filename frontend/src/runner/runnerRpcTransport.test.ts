import { describe, expect, it } from "vitest";

import {
  fragmentCount,
  parseTransportFragment,
  TRANSPORT_PAYLOAD_BYTES_MAX,
  TRANSPORT_REQUEST,
} from "./runnerRpcTransport";

describe("runner RPC transport codec", () => {
  it("uses canonical frame boundaries from the versioned protocol", () => {
    expect(fragmentCount(TRANSPORT_PAYLOAD_BYTES_MAX)).toBe(1);
    expect(fragmentCount(TRANSPORT_PAYLOAD_BYTES_MAX + 1)).toBe(2);

    const payload = Uint8Array.of(1, 2, 3);
    const frame = new Uint8Array(17 + payload.length);
    frame.set(new TextEncoder().encode("YRP1"));
    const view = new DataView(frame.buffer);
    view.setUint8(4, TRANSPORT_REQUEST);
    view.setUint32(5, 0);
    view.setUint32(9, 1);
    view.setUint32(13, payload.length);
    frame.set(payload, 17);

    expect(parseTransportFragment(frame, 10)).toEqual({
      kind: TRANSPORT_REQUEST,
      index: 0,
      count: 1,
      total: 3,
      payload,
    });
  });
});
