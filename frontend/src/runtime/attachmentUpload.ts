import type { RuntimeTransport } from "./runtimeTransport";

const FILE_BYTES_MAX = 50 * 1024 * 1024;
const CHUNK_BYTES = 24_000;
const RESOURCE_ID_PATTERN = /^[0-9a-f]{32}$/u;

export interface SessionAttachment {
  readonly id: string;
  readonly session_id: string;
  readonly filename: string;
  readonly media_type: string;
  readonly size_bytes: number;
  readonly status: "uploading" | "ready";
  readonly next_chunk_index: number;
  readonly received_bytes: number;
}

function base64url(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/gu, "-").replace(/\//gu, "_").replace(/=+$/u, "");
}

function hex(bytes: Uint8Array): string {
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function validateFile(file: File): void {
  if (!file || typeof file.name !== "string" || typeof file.size !== "number") {
    throw new TypeError("Attachment must be a file");
  }
  if (
    !file.name ||
    file.name.length > 255 ||
    file.name !== file.name.trim() ||
    file.name.includes("/") ||
    file.name.includes("\\")
  ) {
    throw new Error("Attachment must have a simple filename");
  }
  if (!Number.isSafeInteger(file.size) || file.size < 1 || file.size > FILE_BYTES_MAX) {
    throw new Error("Attachment must be between 1 byte and 50MB");
  }
}

function validateAttachment(
  value: unknown,
  expected: { sessionId: string; filename: string; sizeBytes: number; id?: string },
): SessionAttachment {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("Attachment response is invalid");
  }
  const attachment = value as Record<string, unknown>;
  if (
    typeof attachment.id !== "string" ||
    !RESOURCE_ID_PATTERN.test(attachment.id) ||
    (expected.id !== undefined && attachment.id !== expected.id) ||
    attachment.session_id !== expected.sessionId ||
    attachment.filename !== expected.filename ||
    attachment.size_bytes !== expected.sizeBytes ||
    typeof attachment.media_type !== "string" ||
    !["uploading", "ready"].includes(String(attachment.status)) ||
    !Number.isSafeInteger(attachment.next_chunk_index) ||
    !Number.isSafeInteger(attachment.received_bytes)
  ) {
    throw new Error("Attachment response did not match the transfer");
  }
  return attachment as unknown as SessionAttachment;
}

export async function uploadSessionAttachment(
  transport: RuntimeTransport,
  sessionId: string,
  file: File,
): Promise<SessionAttachment> {
  validateFile(file);
  if (!RESOURCE_ID_PATTERN.test(sessionId)) {
    throw new Error("Attachment session ID is invalid");
  }
  const fileBytes = new Uint8Array(await file.arrayBuffer());
  if (fileBytes.length !== file.size) {
    fileBytes.fill(0);
    throw new Error("Attachment size changed while reading");
  }
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", fileBytes));
  let attachmentId: string | null = null;
  try {
    const started = validateAttachment(
      await transport.post<unknown>(`/api/sessions/${sessionId}/attachments`, {
        filename: file.name,
        size_bytes: fileBytes.length,
        sha256: hex(digest),
      }),
      { sessionId, filename: file.name, sizeBytes: fileBytes.length },
    );
    attachmentId = started.id;
    const chunkCount = Math.ceil(fileBytes.length / CHUNK_BYTES);
    for (let chunkIndex = 0; chunkIndex < chunkCount; chunkIndex += 1) {
      const offset = chunkIndex * CHUNK_BYTES;
      const chunk = fileBytes.subarray(offset, Math.min(offset + CHUNK_BYTES, fileBytes.length));
      validateAttachment(
        await transport.post<unknown>(
          `/api/sessions/${sessionId}/attachments/${attachmentId}/chunks/${chunkIndex}`,
          { data: base64url(chunk) },
        ),
        { sessionId, filename: file.name, sizeBytes: fileBytes.length, id: attachmentId },
      );
    }
    const completed = validateAttachment(
      await transport.post<unknown>(
        `/api/sessions/${sessionId}/attachments/${attachmentId}/complete`,
      ),
      { sessionId, filename: file.name, sizeBytes: fileBytes.length, id: attachmentId },
    );
    if (completed.status !== "ready" || completed.received_bytes !== fileBytes.length) {
      throw new Error("Attachment completion response is invalid");
    }
    return completed;
  } catch (error) {
    if (attachmentId !== null) {
      try {
        await transport.delete(`/api/sessions/${sessionId}/attachments/${attachmentId}`);
      } catch {
        // A claimed attachment cannot be deleted, while incomplete uploads remain bounded.
      }
    }
    throw error;
  } finally {
    digest.fill(0);
    fileBytes.fill(0);
  }
}
