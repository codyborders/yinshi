// Verifies local macOS packaging remains compatible with the supported Sonoma baseline.

import { readFile } from "node:fs/promises";
import path from "node:path";
import { describe, expect, it } from "vitest";

interface PackageManifest {
  readonly build?: {
    readonly mac?: {
      readonly minimumSystemVersion?: string;
      readonly target?: Array<{
        readonly arch?: string[];
        readonly target?: string;
      }>;
    };
  };
}

describe("desktop packaging configuration", () => {
  it("targets Apple Silicon Macs running macOS Sonoma or newer", async () => {
    const packagePath = path.resolve(process.cwd(), "package.json");
    const manifest = JSON.parse(
      await readFile(packagePath, { encoding: "utf-8" }),
    ) as PackageManifest;

    expect(manifest.build?.mac?.minimumSystemVersion).toBe("14.0");
    expect(manifest.build?.mac?.target).toContainEqual({
      target: "dmg",
      arch: ["arm64"],
    });
  });
});
