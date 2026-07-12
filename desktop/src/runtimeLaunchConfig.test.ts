import { expect, it } from "vitest";

import { buildRuntimeLaunchConfig } from "./runtimeLaunchConfig.js";

it("builds profile-scoped bundled commands without inheriting provider secrets", () => {
  const config = buildRuntimeLaunchConfig({
    resourcesPath: "/Applications/Yinshi.app/Contents/Resources",
    profileDirectoryPath: "/Users/test/Library/Application Support/Yinshi/profiles/profile-hash",
    runtimeSecrets: {
      version: 1,
      secretKey: "s".repeat(64),
      encryptionPepper: "a".repeat(64),
      keyEncryptionKey: "b".repeat(64),
    },
    shellEnvironment: {
      HOME: "/Users/test",
      PATH: "/opt/homebrew/bin:/usr/bin:/bin",
      SHELL: "/bin/zsh",
      SSH_AUTH_SOCK: "/private/tmp/ssh-agent.sock",
      LANG: "en_US.UTF-8",
      OPENAI_API_KEY: "must-not-pass",
      AWS_SECRET_ACCESS_KEY: "must-not-pass",
      NODE_OPTIONS: "--require=/tmp/attack.js",
      DYLD_INSERT_LIBRARIES: "/tmp/attack.dylib",
    },
  });

  expect(config.helper.workingDirectory).toBe(
    "/Users/test/Library/Application Support/Yinshi/profiles/profile-hash",
  );
  expect(config.sidecar.workingDirectory).toBe(
    "/Users/test/Library/Application Support/Yinshi/profiles/profile-hash",
  );
  expect(config.helper.command).toBe(
    "/Applications/Yinshi.app/Contents/Resources/helper/yinshi-desktop-helper",
  );
  expect(config.helper.args).toEqual([
    "--ready-fd",
    "3",
    "--asset-dir",
    "/Applications/Yinshi.app/Contents/Resources/frontend",
  ]);
  expect(config.sidecar.command).toBe(
    "/Applications/Yinshi.app/Contents/Resources/node/bin/node",
  );
  expect(config.sidecar.args).toEqual([
    "/Applications/Yinshi.app/Contents/Resources/sidecar/src/index.js",
  ]);
  expect(config.helper.environment.TENANT_DB_ENCRYPTION).toBe("required");
  expect(config.helper.environment.CONTROL_FIELD_ENCRYPTION).toBe("required");
  expect(config.helper.environment.ENCRYPTION_PEPPER).toBe("a".repeat(64));
  expect(config.helper.environment.KEY_ENCRYPTION_KEY).toBe("b".repeat(64));
  expect(config.helper.environment.ALLOWED_REPO_BASE).toContain("/profile-hash/repositories");
  expect(config.helper.environment.DB_PATH).toContain("/profile-hash/local.db");
  expect(config.helper.environment.CONTROL_DB_PATH).toContain("/profile-hash/control.db");
  expect(config.helper.environment.SIDECAR_SOCKET_PATH).toContain("/profile-hash/run/sidecar.sock");
  expect(config.sidecar.environment.SIDECAR_SOCKET_PATH).toBe(
    config.helper.environment.SIDECAR_SOCKET_PATH,
  );
  expect(config.sidecar.environment.SSH_AUTH_SOCK).toBe("/private/tmp/ssh-agent.sock");

  for (const environment of [config.helper.environment, config.sidecar.environment]) {
    expect(environment).not.toHaveProperty("OPENAI_API_KEY");
    expect(environment).not.toHaveProperty("AWS_SECRET_ACCESS_KEY");
    expect(environment).not.toHaveProperty("NODE_OPTIONS");
    expect(environment).not.toHaveProperty("DYLD_INSERT_LIBRARIES");
  }
});
