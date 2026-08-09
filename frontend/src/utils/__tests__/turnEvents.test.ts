import { describe, expect, it } from "vitest";

import {
  applyTurnEventToBlocks,
  blocksToContent,
  parseStoredTurnBlocks,
  type TurnBlock,
} from "../turnEvents";

describe("turnEvents", () => {
  it("replays stored thinking, tool use, tool output, and assistant text", () => {
    const storedTurn = JSON.stringify({
      schema: "yinshi.assistant_turn.v1",
      events: [
        {
          type: "assistant",
          message: { content: [{ type: "thinking", thinking: "Inspect files." }] },
        },
        {
          type: "tool_use",
          id: "tool-1",
          name: "read",
          input: { path: "README.md" },
        },
        {
          type: "tool_result",
          tool_use_id: "tool-1",
          content: "# Project",
        },
        {
          type: "assistant",
          message: { content: [{ type: "text", text: "Done" }] },
        },
        { type: "result", usage: {} },
      ],
    });
    let blockIndex = 0;

    const blocks = parseStoredTurnBlocks(storedTurn, () => `block-${++blockIndex}`);

    expect(blocks).toEqual([
      { id: "block-1", type: "thinking", text: "Inspect files." },
      {
        id: "tool-1",
        type: "tool_use",
        toolName: "read",
        toolInput: { path: "README.md" },
        toolId: "tool-1",
        toolOutput: "# Project",
        toolError: undefined,
      },
      { id: "block-2", type: "text", text: "Done" },
    ]);
    expect(blocksToContent(blocks)).toBe("Done");
  });

  it("keeps legacy result-only full messages as empty replay blocks", () => {
    const legacyFullMessage = JSON.stringify({
      type: "message",
      data: { type: "result", usage: {} },
    });

    const blocks = parseStoredTurnBlocks(legacyFullMessage, () => "block-1");

    expect(blocks).toEqual([]);
  });

  it("keeps status events visible without adding transcript text", () => {
    const blocks: TurnBlock[] = [];

    const result = applyTurnEventToBlocks(
      blocks,
      {
        type: "status",
        status: "compacting",
        message: "Compacting context...",
        severity: "info",
      },
      () => "block-1",
    );

    expect(result).toEqual({ changed: true });
    expect(blocks).toEqual([
      {
        id: "block-1",
        type: "status",
        text: "Compacting context...",
        severity: "info",
      },
    ]);
    expect(blocksToContent(blocks)).toBe("");
  });

  it("updates streaming tool input from content block deltas", () => {
    const blocks: TurnBlock[] = [];
    let blockIndex = 0;
    const nextBlockId = () => `block-${++blockIndex}`;

    applyTurnEventToBlocks(
      blocks,
      {
        type: "content_block_start",
        content_block: { type: "tool_use", id: "tool-2", name: "bash" },
      },
      nextBlockId,
    );
    applyTurnEventToBlocks(
      blocks,
      {
        type: "content_block_delta",
        delta: { type: "input_json_delta", partial_json: "{\"command\":" },
      },
      nextBlockId,
    );
    applyTurnEventToBlocks(
      blocks,
      {
        type: "content_block_delta",
        delta: { type: "input_json_delta", partial_json: "\"pytest\"}" },
      },
      nextBlockId,
    );
    applyTurnEventToBlocks(blocks, { type: "content_block_stop" }, nextBlockId);

    expect(blocks).toEqual([
      {
        id: "tool-2",
        type: "tool_use",
        toolName: "bash",
        toolInput: { command: "pytest" },
        toolId: "tool-2",
      },
    ]);
  });
});
