import { describe, expect, it, vi } from "vitest";

import { uploadSessionAttachment } from "./attachmentUpload";
import type { RuntimeTransport } from "./runtimeTransport";

const SESSION_ID = "1".repeat(32);
const ATTACHMENT_ID = "a".repeat(32);

describe("uploadSessionAttachment", () => {
  it("uploads ordered chunks and requires a ready completion", async () => {
    const file = new File([new Uint8Array(24_001).fill(7)], "screen.png", {
      type: "image/png",
    });
    let nextChunkIndex = 0;
    const post = vi.fn(async (path: string) => {
      if (path.endsWith("/attachments")) {
        return {
          id: ATTACHMENT_ID,
          session_id: SESSION_ID,
          filename: file.name,
          media_type: "application/octet-stream",
          size_bytes: file.size,
          status: "uploading",
          next_chunk_index: 0,
          received_bytes: 0,
        };
      }
      if (path.endsWith("/complete")) {
        return {
          id: ATTACHMENT_ID,
          session_id: SESSION_ID,
          filename: file.name,
          media_type: "image/png",
          size_bytes: file.size,
          status: "ready",
          next_chunk_index: 2,
          received_bytes: file.size,
        };
      }
      nextChunkIndex += 1;
      return {
        id: ATTACHMENT_ID,
        session_id: SESSION_ID,
        filename: file.name,
        media_type: "application/octet-stream",
        size_bytes: file.size,
        status: "uploading",
        next_chunk_index: nextChunkIndex,
        received_bytes: nextChunkIndex === 1 ? 24_000 : file.size,
      };
    });
    const transport = {
      post,
      delete: vi.fn(),
    } as unknown as RuntimeTransport;

    const attachment = await uploadSessionAttachment(transport, SESSION_ID, file);

    expect(attachment.status).toBe("ready");
    expect(post).toHaveBeenCalledTimes(4);
    expect(post.mock.calls[1]?.[0]).toContain("/chunks/0");
    expect(post.mock.calls[2]?.[0]).toContain("/chunks/1");
  });

  it("deletes a reserved attachment after a transfer failure", async () => {
    const file = new File(["content"], "notes.txt", { type: "text/plain" });
    const remove = vi.fn(async () => undefined);
    const post = vi
      .fn()
      .mockResolvedValueOnce({
        id: ATTACHMENT_ID,
        session_id: SESSION_ID,
        filename: file.name,
        media_type: "application/octet-stream",
        size_bytes: file.size,
        status: "uploading",
        next_chunk_index: 0,
        received_bytes: 0,
      })
      .mockRejectedValueOnce(new Error("transfer failed"));
    const transport = { post, delete: remove } as unknown as RuntimeTransport;

    await expect(uploadSessionAttachment(transport, SESSION_ID, file)).rejects.toThrow(
      "transfer failed",
    );
    expect(remove).toHaveBeenCalledWith(
      `/api/sessions/${SESSION_ID}/attachments/${ATTACHMENT_ID}`,
    );
  });
});
