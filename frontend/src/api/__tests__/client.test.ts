import { afterEach, describe, expect, it, vi } from "vitest";
import {
  ApiError,
  api,
  type ThreadChildCreate,
  type ThreadLimitsOut,
  type ThreadNodeOut,
  type ThreadResultOut,
  type ThreadSpawnOut,
  type ThreadTreeOut,
} from "../client";

describe("api client", () => {
  it("exposes thread API contract types", () => {
    const node: ThreadNodeOut = {
      id: "a",
      delegation_id: null,
      parent_id: null,
      root_id: "a",
      depth: 0,
      title: null,
      role: "general",
      origin: "user",
      state: "running",
      workspace_id: "w",
      model: "m",
      child_count: 0,
      active_child_count: 0,
      can_spawn_child: true,
      created_at: "2026-01-01T00:00:00Z",
    };
    const tree: ThreadTreeOut = {
      root: node,
      nodes: [],
      placeholders: [],
      thread_count: 1,
      active_descendant_count: 0,
      tree_depth: 0,
    };
    const limits: ThreadLimitsOut = {
      max_depth: 1,
      max_direct_children: 1,
      max_active_descendants: 1,
      max_total_threads: 1,
      tree_depth: 0,
      direct_children: 0,
      active_descendants: 0,
      total_threads: 1,
      can_spawn_child: true,
    };
    const spawn: ThreadSpawnOut = {
      delegation_id: "d",
      status: "provisioning",
      child_session_id: null,
      error_code: null,
    };
    const result: ThreadResultOut = {
      delegation_id: "d",
      version: 1,
      source: "reported",
      sealed: false,
      summary: "done",
      tests: [],
      warnings: [],
      base_commit: null,
      result_commit: null,
      result_ref: null,
      changed_files: [],
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z",
      sealed_at: null,
    };
    const create: ThreadChildCreate = {
      idempotency_key: "key",
      title: "title",
      task: "task",
    };
    expect({ tree, limits, spawn, result, create }).toBeDefined();
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("parses structured GitHub access errors", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            detail: {
              code: "github_connect_required",
              message: "Connect GitHub to import this private repository.",
              connect_url: "/auth/github/install",
              manage_url: null,
            },
          }),
          {
            status: 400,
            headers: { "Content-Type": "application/json" },
          },
        ),
      ),
    );

    let caughtError: unknown;
    try {
      await api.get("/api/repos");
    } catch (error) {
      caughtError = error;
    }

    expect(caughtError).toBeInstanceOf(ApiError);
    expect(caughtError).toMatchObject({
      code: "github_connect_required",
      connectUrl: "/auth/github/install",
      manageUrl: null,
      message: "Connect GitHub to import this private repository.",
      status: 400,
    });
  });

  it("uploads multipart form data for Pi config archives", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({ id: "cfg-1", status: "ready" }),
        {
          status: 200,
          headers: { "Content-Type": "application/json" },
        },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const file = new File(["zip-bytes"], "pi-config.zip", {
      type: "application/zip",
    });
    const result = await api.upload<{ id: string; status: string }>(
      "/api/settings/pi-config/upload",
      file,
    );

    expect(result).toEqual({ id: "cfg-1", status: "ready" });
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/settings/pi-config/upload",
      expect.objectContaining({
        method: "POST",
        credentials: "include",
      }),
    );
  });
});
