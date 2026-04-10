import fs from "node:fs";
import test from "node:test";
import assert from "node:assert/strict";

import {
  createGitAskpassBundle,
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

  assert.equal(parsedCommand?.command, "git");
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

test("createGitAskpassBundle uses a unique private bundle path", () => {
  // Unique bundle paths prevent later commands from overwriting a shared helper.
  const firstBundle = createGitAskpassBundle("token-1");
  const secondBundle = createGitAskpassBundle("token-2");

  try {
    assert.notEqual(firstBundle.askpassPath, secondBundle.askpassPath);
    assert.notEqual(firstBundle.bundleDirPath, secondBundle.bundleDirPath);
    assert.equal(fs.existsSync(firstBundle.askpassPath), true);
    assert.equal(fs.existsSync(secondBundle.askpassPath), true);
  } finally {
    firstBundle.cleanup();
    secondBundle.cleanup();
  }

  assert.equal(fs.existsSync(firstBundle.bundleDirPath), false);
  assert.equal(fs.existsSync(secondBundle.bundleDirPath), false);
});
