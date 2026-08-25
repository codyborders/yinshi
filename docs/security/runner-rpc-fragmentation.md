# Runner RPC Fragmentation

Status: version 1

## Header

Version 1 uses a 17-byte network-order header:

```text
magic[4] || kind[1] || index[4] || count[4] || total[4]
```

`magic` is ASCII `YRP1`. Integer fields are unsigned and big-endian. Frame kinds are request `1`, acknowledgement `2`, response `3`, and pull `4`.

## Limits

Maximum Noise ciphertext size is 65,535 bytes. Each Noise tag uses 16 bytes. A fragmented payload carries at most 65,502 bytes after its 17-byte header. RPC requests are limited to 2 MiB. Complete RPC responses are limited to 10 MiB.

## Exchange

Request fragments start at index zero and arrive in order. Each non-final request fragment gets an empty acknowledgement. Its index must match. The count and total fields must also match.

Final request fragments get the first response fragment. Clients get later response fragments with empty pull frames in exact order. A request with `response_mode: push` receives every response fragment immediately without pulls.

Fragment count is `max(1, ceil(total / 65502))`. Each fragment except the last carries 65,502 payload bytes. The last fragment carries the exact remainder.

Invalid counts or indexes fail the transfer. Invalid totals, payload lengths, or frame kinds also fail it.

## Implementations

Canonical Python constants and header codec are in `backend/src/yinshi/services/runner_rpc_transport.py`. Browser codec is in `frontend/src/runner/runnerRpcTransport.ts`. Both endpoint test suites use these codec modules.
