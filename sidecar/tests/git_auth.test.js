import fs from "node:fs";
import path from "node:path";
import { spawn } from "node:child_process";
import test from "node:test";
import assert from "node:assert/strict";

import {
  createGitAskpassBundle,
  createGitCredentialBroker,
  parseGitCommandForRuntimeAuth,
  tokenizeShellCommand,
} from "../src/git_auth.js";

test("tokenizeShellCommand keeps direct git tokens simple", () => {
  // This test keeps the supported grammar explicit so auth stays out of
  // general-purpose shell commands.
  const tokens = tokenizeShellCommand("cd './repo dir' && git push origin main");

  assert.deepEqual(tokens, ["cd", "./repo dir", "&&", "git", "push", "origin", "main"]);
});

test("parseGitCommandForRuntimeAuth accepts a direct remote git command", () => {
  // Only direct git remote operations should receive runtime auth.
  const parsedCommand = parseGitCommandForRuntimeAuth(
    "cd ./repo && git push origin main",
    "/tmp/workspace",
  );

  assert.equal(parsedCommand?.command, "/usr/bin/git");
  assert.equal(parsedCommand?.cwd, "/tmp/workspace/repo");
  assert.deepEqual(parsedCommand?.gitArguments, ["push", "origin", "main"]);
});

test("parseGitCommandForRuntimeAuth rejects shell chaining after git", () => {
  // Rejecting chained commands prevents arbitrary shell code from inheriting auth.
  const parsedCommand = parseGitCommandForRuntimeAuth(
    "git push origin main && env",
    "/tmp/workspace",
  );

  assert.equal(parsedCommand, null);
});

test("parseGitCommandForRuntimeAuth rejects non-git shell commands", () => {
  // Commands that merely mention git must not become authenticated.
  const parsedCommand = parseGitCommandForRuntimeAuth(
    "printf git; env | grep YINSHI_GIT_TOKEN",
    "/tmp/workspace",
  );

  assert.equal(parsedCommand, null);
});

function runAskpass(credentialBroker, prompt, includeCapability) {
  return new Promise((resolve, reject) => {
    const child = spawn(credentialBroker.askpassPath, [prompt], {
      env: {
        PATH: process.env.PATH,
        YINSHI_GIT_CREDENTIAL_SOCKET: credentialBroker.socketPath,
      },
      stdio: ["ignore", "pipe", "pipe", includeCapability ? credentialBroker.capabilityFd : "ignore"],
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => { stdout += chunk.toString("utf-8"); });
    child.stderr.on("data", (chunk) => { stderr += chunk.toString("utf-8"); });
    child.on("error", reject);
    child.on("close", (exitCode) => resolve({ exitCode, stdout, stderr }));
  });
}

function fillGitCredential(credentialBroker) {
  return new Promise((resolve, reject) => {
    const child = spawn(
      "/usr/bin/git",
      ["-c", "credential.helper=", "credential", "fill"],
      {
        env: {
          GIT_ASKPASS: credentialBroker.askpassPath,
          GIT_TERMINAL_PROMPT: "0",
          PATH: process.env.PATH,
          YINSHI_GIT_CREDENTIAL_SOCKET: credentialBroker.socketPath,
        },
        stdio: ["pipe", "pipe", "pipe", credentialBroker.capabilityFd],
      },
    );
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => { stdout += chunk.toString("utf-8"); });
    child.stderr.on("data", (chunk) => { stderr += chunk.toString("utf-8"); });
    child.on("error", reject);
    child.on("close", (exitCode) => resolve({ exitCode, stdout, stderr }));
    child.stdin.end("protocol=https\nhost=github.com\n\n");
  });
}

test("credential capability survives Git askpass execution", async () => {
  const credentialBroker = await createGitCredentialBroker("inert-git-credential", "github.com");
  try {
    const result = await fillGitCredential(credentialBroker);

    assert.equal(result.exitCode, 0, result.stderr);
    assert.match(result.stdout, /username=x-access-token/);
    assert.match(result.stdout, /password=inert-git-credential/);
  } finally {
    credentialBroker.cleanup();
  }
});

test("credential broker denies issuance for hosts other than the approved host", async () => {
  // An attacker-controlled remote, an HTTP redirect, or an insteadOf
  // rewrite must never receive the scoped installation token.
  const credentialBroker = await createGitCredentialBroker("inert-credential", "github.com");
  try {
    const redirectedHost = await runAskpass(
      credentialBroker,
      "Password for 'https://evil.example.com': ",
      true,
    );
    assert.notEqual(redirectedHost.exitCode, 0);
    assert.doesNotMatch(redirectedHost.stdout, /inert-credential/);

    const rewrittenTarget = await runAskpass(
      credentialBroker,
      "Password for 'https://x-access-token@evil.example.com': ",
      true,
    );
    assert.notEqual(rewrittenTarget.exitCode, 0);
    assert.doesNotMatch(rewrittenTarget.stdout, /inert-credential/);

    const usernameProbe = await runAskpass(
      credentialBroker,
      "Username for 'https://evil.example.com': ",
      true,
    );
    assert.notEqual(usernameProbe.exitCode, 0);
    assert.doesNotMatch(usernameProbe.stdout, /x-access-token/);
  } finally {
    credentialBroker.cleanup();
  }
});

test("credential broker requires an inherited capability and issues once", async () => {
  const credentialBroker = await createGitCredentialBroker("inert-credential", "github.com");
  try {
    assert.deepEqual(
      fs.readdirSync(path.dirname(credentialBroker.askpassPath)).sort(),
      ["askpass.sh", "credential.sock"],
    );
    const denied = await runAskpass(credentialBroker, "Password for GitHub", false);
    assert.notEqual(denied.exitCode, 0);
    assert.doesNotMatch(denied.stdout, /inert-credential/);

    const username = await runAskpass(
      credentialBroker,
      "Username for 'https://github.com': ",
      true,
    );
    assert.equal(username.exitCode, 0);
    assert.equal(username.stdout.trim(), "x-access-token");

    const password = await runAskpass(
      credentialBroker,
      "Password for 'https://x-access-token@github.com': ",
      true,
    );
    assert.equal(password.exitCode, 0);
    assert.equal(password.stdout.trim(), "inert-credential");

    const replay = await runAskpass(
      credentialBroker,
      "Password for 'https://x-access-token@github.com': ",
      true,
    );
    assert.notEqual(replay.exitCode, 0);
    assert.doesNotMatch(replay.stdout, /inert-credential/);
  } finally {
    credentialBroker.cleanup();
  }
});

test("createGitAskpassBundle contains no credential material", () => {
  const firstBundle = createGitAskpassBundle();
  const secondBundle = createGitAskpassBundle();

  try {
    assert.notEqual(firstBundle.askpassPath, secondBundle.askpassPath);
    assert.notEqual(firstBundle.bundleDirPath, secondBundle.bundleDirPath);
    assert.equal(fs.statSync(firstBundle.bundleDirPath).mode & 0o777, 0o700);
    assert.equal(fs.statSync(firstBundle.askpassPath).mode & 0o777, 0o700);
    assert.deepEqual(fs.readdirSync(firstBundle.bundleDirPath), ["askpass.sh"]);
    assert.doesNotMatch(fs.readFileSync(firstBundle.askpassPath, "utf-8"), /token-1/);
  } finally {
    firstBundle.cleanup();
    secondBundle.cleanup();
  }

  assert.equal(fs.existsSync(firstBundle.bundleDirPath), false);
  assert.equal(fs.existsSync(secondBundle.bundleDirPath), false);
});
