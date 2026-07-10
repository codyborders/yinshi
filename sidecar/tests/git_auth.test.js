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
