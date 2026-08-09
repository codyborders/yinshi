import { execFile } from "node:child_process";
import { promisify } from "node:util";

export interface FileVaultStatus {
  readonly status: "disabled" | "enabled" | "unknown";
}

interface ExecuteResult {
  readonly stdout: string;
  readonly stderr: string;
}

type Execute = (
  file: string,
  arguments_: readonly string[],
  options: { readonly timeout: number; readonly maxBuffer: number; readonly windowsHide: boolean },
) => Promise<ExecuteResult>;

const executeFile = promisify(execFile) as unknown as Execute;

export async function detectFileVaultStatus(
  execute: Execute = executeFile,
): Promise<FileVaultStatus> {
  if (typeof execute !== "function") {
    throw new TypeError("FileVault execute dependency must be callable");
  }
  try {
    const result = await execute("/usr/bin/fdesetup", ["status"], {
      timeout: 5_000,
      maxBuffer: 16_384,
      windowsHide: true,
    });
    if (result.stderr.trim()) return { status: "unknown" };
    const output = result.stdout.trim();
    if (output === "FileVault is On.") return { status: "enabled" };
    if (output === "FileVault is Off.") return { status: "disabled" };
    return { status: "unknown" };
  } catch {
    return { status: "unknown" };
  }
}
