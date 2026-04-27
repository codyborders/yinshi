import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import test from "node:test";

const gitGuardPath = path.resolve("bin", "git");
const realGitPath = "/usr/bin/git";

function runGit(args, cwd) {
  return spawnSync(gitGuardPath, args, {
    cwd,
    encoding: "utf8",
  });
}

function runRealGit(args, cwd) {
  return spawnSync(realGitPath, args, {
    cwd,
    encoding: "utf8",
  });
}

function createRepo() {
  const repoPath = fs.mkdtempSync(path.join(os.tmpdir(), "yinshi-git-guard-test-"));
  assert.equal(runGit(["init"], repoPath).status, 0);
  assert.equal(runGit(["config", "user.email", "test@example.com"], repoPath).status, 0);
  assert.equal(runGit(["config", "user.name", "Test User"], repoPath).status, 0);
  fs.writeFileSync(path.join(repoPath, "README.md"), "# Test\n", "utf8");
  assert.equal(runGit(["add", "README.md"], repoPath).status, 0);
  assert.equal(runGit(["commit", "-m", "init"], repoPath).status, 0);
  return repoPath;
}

test("git guard blocks env commits even when hooks are disabled", () => {
  const repoPath = createRepo();
  try {
    fs.writeFileSync(path.join(repoPath, ".env"), "TOKEN=secret\n", "utf8");
    assert.equal(runGit(["add", "-f", ".env"], repoPath).status, 0);

    const commit = runGit(["commit", "--no-verify", "-m", "try env"], repoPath);

    assert.notEqual(commit.status, 0);
    assert.match(commit.stderr, /Yinshi blocks Git operations/);
  } finally {
    fs.rmSync(repoPath, { recursive: true, force: true });
  }
});

test("git guard blocks pushes with tracked env files before contacting remote", () => {
  const repoPath = createRepo();
  try {
    fs.writeFileSync(path.join(repoPath, ".env"), "TOKEN=secret\n", "utf8");
    assert.equal(runRealGit(["add", "-f", ".env"], repoPath).status, 0);
    assert.equal(runRealGit(["commit", "--no-verify", "-m", "force env"], repoPath).status, 0);

    const push = runGit(["push", "https://example.invalid/repo.git", "HEAD:main"], repoPath);

    assert.notEqual(push.status, 0);
    assert.match(push.stderr, /Yinshi blocks Git operations/);
  } finally {
    fs.rmSync(repoPath, { recursive: true, force: true });
  }
});
