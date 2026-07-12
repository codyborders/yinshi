import path from "node:path";

import type { RuntimeSecrets } from "./runtimeSecrets.js";

const SHELL_ENVIRONMENT_KEYS = [
  "HOME",
  "LANG",
  "LC_ALL",
  "LC_CTYPE",
  "LOGNAME",
  "PATH",
  "SHELL",
  "SSH_AUTH_SOCK",
  "TMPDIR",
  "USER",
] as const;

export interface RuntimeLaunchConfigOptions {
  readonly resourcesPath: string;
  readonly profileDirectoryPath: string;
  readonly runtimeSecrets: RuntimeSecrets;
  readonly shellEnvironment: Readonly<Record<string, string | undefined>>;
}

export interface ChildLaunchConfig {
  readonly command: string;
  readonly workingDirectory: string;
  readonly args: readonly string[];
  readonly environment: Readonly<Record<string, string>>;
}

export interface RuntimeLaunchConfig {
  readonly helper: ChildLaunchConfig;
  readonly sidecar: ChildLaunchConfig;
}

function requireAbsolutePath(value: string, name: string): string {
  if (typeof value !== "string" || !path.isAbsolute(value) || value.includes("\0")) {
    throw new TypeError(`${name} must be an absolute path`);
  }
  return path.normalize(value);
}

function copyShellEnvironment(
  source: Readonly<Record<string, string | undefined>>,
): Record<string, string> {
  if (typeof source !== "object" || source === null || Array.isArray(source)) {
    throw new TypeError("shellEnvironment must be an object");
  }
  const environment: Record<string, string> = {};
  for (const key of SHELL_ENVIRONMENT_KEYS) {
    const value = source[key];
    if (value === undefined || value === "") {
      continue;
    }
    if (typeof value !== "string" || value.includes("\0")) {
      throw new TypeError(`shell environment ${key} is invalid`);
    }
    environment[key] = value;
  }
  for (const requiredKey of ["HOME", "PATH", "SHELL"] as const) {
    if (environment[requiredKey] === undefined) {
      throw new Error(`shell environment ${requiredKey} is required`);
    }
  }
  return environment;
}

function validateRuntimeSecrets(secrets: RuntimeSecrets): void {
  if (secrets.version !== 1 || !/^[A-Za-z0-9_-]{64}$/.test(secrets.secretKey)) {
    throw new Error("runtime session secret is invalid");
  }
  if (!/^[a-f0-9]{64}$/.test(secrets.encryptionPepper)) {
    throw new Error("runtime encryption pepper is invalid");
  }
  if (!/^[a-f0-9]{64}$/.test(secrets.keyEncryptionKey)) {
    throw new Error("runtime key-encryption key is invalid");
  }
}

export function buildRuntimeLaunchConfig(
  options: RuntimeLaunchConfigOptions,
): RuntimeLaunchConfig {
  const resourcesPath = requireAbsolutePath(options.resourcesPath, "resourcesPath");
  const profileDirectoryPath = requireAbsolutePath(
    options.profileDirectoryPath,
    "profileDirectoryPath",
  );
  validateRuntimeSecrets(options.runtimeSecrets);
  const shellEnvironment = copyShellEnvironment(options.shellEnvironment);

  const runDirectoryPath = path.join(profileDirectoryPath, "run");
  const sidecarSocketPath = path.join(runDirectoryPath, "sidecar.sock");
  const bundledBinaryPath = path.join(resourcesPath, "bin");
  const inheritedPath = shellEnvironment.PATH;
  if (inheritedPath === undefined) {
    throw new Error("shell environment PATH is required");
  }
  const childPath = `${bundledBinaryPath}:${inheritedPath}`;
  const sharedEnvironment = {
    ...shellEnvironment,
    PATH: childPath,
    SIDECAR_SOCKET_PATH: sidecarSocketPath,
    YINSHI_DESKTOP: "1",
  };
  const helperEnvironment = Object.freeze({
    ...sharedEnvironment,
    ALLOWED_REPO_BASE: path.join(profileDirectoryPath, "repositories"),
    APP_NAME: "Yinshi",
    BACKUP_DIR: path.join(profileDirectoryPath, "backups"),
    CONTAINER_ENABLED: "false",
    CONTAINER_SOCKET_BASE: runDirectoryPath,
    CONTROL_DB_PATH: path.join(profileDirectoryPath, "control.db"),
    CONTROL_FIELD_ENCRYPTION: "required",
    DB_PATH: path.join(profileDirectoryPath, "local.db"),
    DEBUG: "false",
    DISABLE_AUTH: "true",
    ENCRYPTION_PEPPER: options.runtimeSecrets.encryptionPepper,
    HSTS_ENABLED: "false",
    KEY_ENCRYPTION_KEY: options.runtimeSecrets.keyEncryptionKey,
    KEY_ENCRYPTION_KEY_ID: "desktop-v1",
    PYTHONUNBUFFERED: "1",
    REQUIRE_HTTPS: "disabled",
    SECRET_KEY: options.runtimeSecrets.secretKey,
    TENANT_DB_ENCRYPTION: "required",
    TRUSTED_HOSTS: "127.0.0.1,localhost",
    USER_DATA_DIR: path.join(profileDirectoryPath, "data"),
    USER_DATA_ENCRYPTION: "disabled",
  });
  const sidecarEnvironment = Object.freeze({
    ...sharedEnvironment,
    NODE_ENV: "production",
    SIDECAR_LOAD_DOTENV: "0",
  });
  return {
    helper: {
      command: path.join(resourcesPath, "helper", "yinshi-desktop-helper"),
      workingDirectory: profileDirectoryPath,
      args: Object.freeze([
        "--ready-fd",
        "3",
        "--asset-dir",
        path.join(resourcesPath, "frontend"),
      ]),
      environment: helperEnvironment,
    },
    sidecar: {
      command: path.join(resourcesPath, "node", "bin", "node"),
      workingDirectory: profileDirectoryPath,
      args: Object.freeze([path.join(resourcesPath, "sidecar", "src", "index.js")]),
      environment: sidecarEnvironment,
    },
  };
}
