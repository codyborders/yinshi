import type { RuntimeRef } from "./runtimeTransport";

const RESOURCE_ID_PATTERN = /^[0-9a-f]{32}$/;
const RUNNER_ID_LENGTH_MAX = 256;

export type UnresolvedRuntimeRef =
  | { readonly location: "local" }
  | { readonly location: "hosted" }
  | {
      readonly location: "byoc";
      readonly runnerId: string;
      readonly runnerPublicKey: null;
    };

export interface ParsedRuntimeResourceId {
  readonly runtime: UnresolvedRuntimeRef;
  readonly resourceId: string;
}

function encodeBase64url(value: string): string {
  const bytes = new TextEncoder().encode(value);
  let binary = "";
  for (const byte of bytes) {
    binary += String.fromCharCode(byte);
  }
  return btoa(binary).replace(/\+/gu, "-").replace(/\//gu, "_").replace(/=+$/u, "");
}

function decodeBase64url(value: string): string {
  if (!/^[A-Za-z0-9_-]+$/u.test(value)) {
    throw new Error("Runtime runner qualifier is invalid");
  }
  const padded = value.replace(/-/gu, "+").replace(/_/gu, "/").padEnd(
    Math.ceil(value.length / 4) * 4,
    "=",
  );
  let binary: string;
  try {
    binary = atob(padded);
  } catch {
    throw new Error("Runtime runner qualifier is invalid");
  }
  const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0));
  let decoded: string;
  try {
    decoded = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    throw new Error("Runtime runner qualifier is invalid");
  }
  if (!decoded || decoded.length > RUNNER_ID_LENGTH_MAX || encodeBase64url(decoded) !== value) {
    throw new Error("Runtime runner qualifier is invalid");
  }
  return decoded;
}

function validateResourceId(resourceId: string): void {
  if (!RESOURCE_ID_PATTERN.test(resourceId)) {
    throw new Error("Runtime resource ID is invalid");
  }
}

export function defaultRuntimeRef({ desktop }: { desktop: boolean }): UnresolvedRuntimeRef {
  if (typeof desktop !== "boolean") {
    throw new TypeError("desktop must be a boolean");
  }
  return desktop ? { location: "local" } : { location: "hosted" };
}

export function runtimeResourceId(
  runtime: RuntimeRef,
  resourceId: string,
  environment: { readonly desktop: boolean } = { desktop: false },
): string {
  validateResourceId(resourceId);
  if (runtime.location === "hosted") {
    return environment.desktop ? `hosted.${resourceId}` : resourceId;
  }
  if (runtime.location === "local") {
    return `local.${resourceId}`;
  }
  if (!runtime.runnerId || runtime.runnerId.length > RUNNER_ID_LENGTH_MAX) {
    throw new Error("BYOC runner ID is invalid");
  }
  return `byoc.${encodeBase64url(runtime.runnerId)}.${resourceId}`;
}

export function parseRuntimeResourceId(
  encodedId: string,
  environment: { readonly desktop: boolean },
): ParsedRuntimeResourceId {
  if (typeof encodedId !== "string" || !encodedId) {
    throw new Error("Runtime resource ID is invalid");
  }
  if (!encodedId.includes(".")) {
    validateResourceId(encodedId);
    return {
      runtime: defaultRuntimeRef(environment),
      resourceId: encodedId,
    };
  }
  const parts = encodedId.split(".");
  if (parts.length === 2 && parts[0] === "hosted") {
    validateResourceId(parts[1]);
    return { runtime: { location: "hosted" }, resourceId: parts[1] };
  }
  if (parts.length === 2 && parts[0] === "local") {
    validateResourceId(parts[1]);
    return { runtime: { location: "local" }, resourceId: parts[1] };
  }
  if (parts.length === 3 && parts[0] === "byoc") {
    validateResourceId(parts[2]);
    return {
      runtime: {
        location: "byoc",
        runnerId: decodeBase64url(parts[1]),
        runnerPublicKey: null,
      },
      resourceId: parts[2],
    };
  }
  throw new Error("Runtime resource ID qualifier is invalid");
}
