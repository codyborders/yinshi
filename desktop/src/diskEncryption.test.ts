// Covers bounded FileVault status detection without shell execution.

import { describe, expect, it, vi } from "vitest";

import { detectFileVaultStatus } from "./diskEncryption.js";

describe("detectFileVaultStatus", () => {
  it("reports enabled only for the exact FileVault status", async () => {
    const execute = vi.fn().mockResolvedValue({
      stdout: "FileVault is On.\n",
      stderr: "",
    });

    await expect(detectFileVaultStatus(execute)).resolves.toEqual({
      status: "enabled",
    });
    expect(execute).toHaveBeenCalledWith("/usr/bin/fdesetup", ["status"], {
      timeout: 5_000,
      maxBuffer: 16_384,
      windowsHide: true,
    });
  });

  it("distinguishes disabled from an unavailable status", async () => {
    await expect(
      detectFileVaultStatus(async () => ({
        stdout: "FileVault is Off.\n",
        stderr: "",
      })),
    ).resolves.toEqual({ status: "disabled" });
    await expect(
      detectFileVaultStatus(async () => {
        throw new Error("unavailable");
      }),
    ).resolves.toEqual({ status: "unknown" });
  });
});
