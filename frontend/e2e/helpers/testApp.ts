import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { randomUUID } from "node:crypto";
import type { BrowserContext } from "@playwright/test";

import {
  authCookieScript,
  backendBaseUrl,
  backendEnv,
  backendPython,
  repoBaseDir,
} from "./config";

export interface AuthSession {
  email: string;
  token: string;
  userId: string;
}

export interface SeededStack {
  repo: { id: string; name: string };
  workspace: { id: string; name: string; branch: string };
  session: { id: string; model: string };
}

function cookieHeader(session: AuthSession): string {
  return `yinshi_session=${session.token}`;
}

export function uniqueEmail(label = "playwright"): string {
  return `${label}-${randomUUID()}@example.com`;
}

export function createAuthSession(email = uniqueEmail()): AuthSession {
  const raw = execFileSync(backendPython, [authCookieScript, email], {
    env: backendEnv,
    encoding: "utf-8",
  });
  return JSON.parse(raw) as AuthSession;
}

export async function authenticateContext(
  context: BrowserContext,
  email = uniqueEmail(),
): Promise<AuthSession> {
  const session = createAuthSession(email);
  await context.addCookies([
    {
      name: "yinshi_session",
      value: session.token,
      domain: "127.0.0.1",
      path: "/",
      httpOnly: true,
      secure: false,
      sameSite: "Lax",
    },
  ]);
  return session;
}

export function createLocalRepo(prefix = "repo"): string {
  const repoName = `${prefix}-${randomUUID().slice(0, 8)}`;
  const repoPath = path.join(repoBaseDir, repoName);

  fs.rmSync(repoPath, { recursive: true, force: true });
  fs.mkdirSync(repoPath, { recursive: true });
  fs.writeFileSync(
    path.join(repoPath, "README.md"),
    `# ${repoName}\n\nCreated for Playwright.\n`,
    "utf-8",
  );

  execFileSync("git", ["init", repoPath], { encoding: "utf-8" });
  execFileSync("git", ["config", "user.email", "playwright@example.com"], {
    cwd: repoPath,
    encoding: "utf-8",
  });
  execFileSync("git", ["config", "user.name", "Playwright"], {
    cwd: repoPath,
    encoding: "utf-8",
  });
  execFileSync("git", ["add", "."], { cwd: repoPath, encoding: "utf-8" });
  execFileSync("git", ["commit", "-m", "init"], {
    cwd: repoPath,
    encoding: "utf-8",
  });

  return repoPath;
}

async function backendRequest<T>(
  session: AuthSession,
  pathName: string,
  method: string,
  body?: unknown,
): Promise<T> {
  const response = await fetch(`${backendBaseUrl}${pathName}`, {
    method,
    headers: {
      "Content-Type": "application/json",
      "X-Requested-With": "XMLHttpRequest",
      Cookie: cookieHeader(session),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });

  if (!response.ok) {
    const text = await response.text();
    throw new Error(`${method} ${pathName} failed: ${response.status} ${text}`);
  }

  if (response.status === 204) {
    return undefined as T;
  }

  return (await response.json()) as T;
}

export async function seedFullStack(
  session: AuthSession,
  repoPath: string,
): Promise<SeededStack> {
  const repo = await backendRequest<{ id: string; name: string }>(
    session,
    "/api/repos",
    "POST",
    {
      name: path.basename(repoPath),
      local_path: repoPath,
    },
  );
  const workspace = await backendRequest<{
    id: string;
    name: string;
    branch: string;
  }>(session, `/api/repos/${repo.id}/workspaces`, "POST", {});
  const seedSession = await backendRequest<{ id: string; model: string }>(
    session,
    `/api/workspaces/${workspace.id}/sessions`,
    "POST",
    {},
  );

  return {
    repo,
    workspace,
    session: seedSession,
  };
}
