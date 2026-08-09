// Verifies local macOS packaging remains compatible with the supported Sonoma baseline.

import { readFile } from "node:fs/promises";
import path from "node:path";
import { build } from "esbuild";
import { describe, expect, it } from "vitest";

interface PackageManifest {
  readonly scripts?: Readonly<Record<string, string>>;
  readonly build?: {
    readonly extraResources?: Array<{
      readonly from?: string;
      readonly to?: string;
      readonly filter?: string[];
    }>;
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
    expect(manifest.build?.extraResources).toContainEqual({
      from: "runtime/sidecar/node_modules",
      to: "sidecar/node_modules",
      filter: ["**/*"],
    });
    expect(manifest.scripts?.["build:helper"]).toContain("YINSHI_TEST_PYTHON");
    expect(manifest.scripts?.["build:helper"]).toContain(".venv/bin/python");
  });

  it("bundles the sandbox preload as CommonJS and loads that artifact", async () => {
    const result = await build({
      bundle: true,
      entryPoints: [path.resolve(process.cwd(), "src/preload.ts")],
      external: ["electron"],
      format: "cjs",
      platform: "node",
      write: false,
    });
    const output = result.outputFiles?.[0]?.text;
    const mainSource = await readFile(
      path.resolve(process.cwd(), "src/main.ts"),
      {
        encoding: "utf-8",
      },
    );

    expect(output).toBeDefined();
    expect(output).toContain('require("electron")');
    expect(output).not.toMatch(/^import\s/mu);
    expect(mainSource).toContain('path.join(moduleDirectory, "preload.cjs")');
  });
});
