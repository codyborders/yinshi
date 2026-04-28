import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawn } from "node:child_process";

import { createBashTool, createLocalBashOperations } from "@mariozechner/pi-coding-agent";

const GIT_REMOTE_SUBCOMMANDS = new Set(["clone", "fetch", "ls-remote", "pull", "push"]);
const SHELL_AND_OPERATOR = "&&";

function assertNonEmptyString(value, name) {
  if (typeof value !== "string") {
    throw new TypeError(`${name} must be a string`);
  }
  const normalizedValue = value.trim();
  if (!normalizedValue) {
    throw new Error(`${name} must be a non-empty string`);
  }
  return normalizedValue;
}

function quoteShellLiteral(value) {
  const normalizedValue = assertNonEmptyString(value, "value");
  return `'${normalizedValue.replace(/'/g, `'\"'\"'`)}'`;
}

function buildGitAskpassScript(tokenFilePath) {
  const normalizedTokenFilePath = assertNonEmptyString(tokenFilePath, "tokenFilePath");
  return "#!/bin/sh\n"
    + "case \"$1\" in\n"
    + "  *Username*) printf '%s\\n' 'x-access-token' ;;\n"
    + `  *) cat ${quoteShellLiteral(normalizedTokenFilePath)} ;;\n`
    + "esac\n";
}

export function normalizeGitAuth(gitAuth) {
  if (gitAuth === null || gitAuth === undefined) {
    return null;
  }
  if (typeof gitAuth !== "object" || Array.isArray(gitAuth)) {
    throw new TypeError("gitAuth must be an object");
  }

  if (gitAuth.strategy !== "github_app_https") {
    throw new Error(`Unsupported git auth strategy: ${gitAuth.strategy}`);
  }
  const normalizedHost = assertNonEmptyString(gitAuth.host, "gitAuth.host");
  if (normalizedHost !== "github.com") {
    throw new Error("gitAuth.host must be github.com");
  }
  const normalizedAccessToken = assertNonEmptyString(
    gitAuth.accessToken,
    "gitAuth.accessToken",
  );
  return {
    strategy: gitAuth.strategy,
    host: normalizedHost,
    accessToken: normalizedAccessToken,
  };
}

export function tokenizeShellCommand(command) {
  const normalizedCommand = assertNonEmptyString(command, "command");
  const tokens = [];
  let currentToken = "";
  let quoteMode = null;

  const pushCurrentToken = () => {
    if (!currentToken) {
      return;
    }
    tokens.push(currentToken);
    currentToken = "";
  };

  for (let index = 0; index < normalizedCommand.length; index += 1) {
    const character = normalizedCommand[index];

    if (quoteMode === "single") {
      if (character === "'") {
        quoteMode = null;
      } else {
        currentToken += character;
      }
      continue;
    }
    if (quoteMode === "double") {
      if (character === "\"") {
        quoteMode = null;
        continue;
      }
      if (character === "\\") {
        index += 1;
        if (index >= normalizedCommand.length) {
          throw new Error("command ends with an incomplete escape sequence");
        }
        currentToken += normalizedCommand[index];
        continue;
      }
      currentToken += character;
      continue;
    }

    if (character === "'") {
      quoteMode = "single";
      continue;
    }
    if (character === "\"") {
      quoteMode = "double";
      continue;
    }
    if (character === "\\") {
      index += 1;
      if (index >= normalizedCommand.length) {
        throw new Error("command ends with an incomplete escape sequence");
      }
      currentToken += normalizedCommand[index];
      continue;
    }
    if (/\s/.test(character)) {
      pushCurrentToken();
      continue;
    }
    if (character === "&") {
      if (normalizedCommand[index + 1] !== "&") {
        throw new Error("unsupported shell operator: &");
      }
      pushCurrentToken();
      tokens.push(SHELL_AND_OPERATOR);
      index += 1;
      continue;
    }
    if (character === ";" || character === "|" || character === ">" || character === "<" || character === "`") {
      throw new Error(`unsupported shell operator: ${character}`);
    }
    if (character === "$") {
      throw new Error("unsupported shell expansion");
    }

    currentToken += character;
  }

  if (quoteMode !== null) {
    throw new Error("command contains an unterminated quote");
  }

  pushCurrentToken();
  return tokens;
}

function resolveGitWorkingDirectory(tokens, defaultCwd) {
  const normalizedDefaultCwd = assertNonEmptyString(defaultCwd, "defaultCwd");
  if (tokens.length === 0) {
    return { gitCwd: normalizedDefaultCwd, gitIndex: 0 };
  }
  if (tokens[0] !== "cd") {
    return { gitCwd: normalizedDefaultCwd, gitIndex: 0 };
  }
  if (tokens.length < 4) {
    return null;
  }
  if (tokens[2] !== SHELL_AND_OPERATOR) {
    return null;
  }
  if (!tokens[1]) {
    return null;
  }

  return {
    gitCwd: path.resolve(normalizedDefaultCwd, tokens[1]),
    gitIndex: 3,
  };
}

export function parseGitCommandForRuntimeAuth(command, defaultCwd) {
  let tokens = null;
  try {
    tokens = tokenizeShellCommand(command);
  } catch {
    return null;
  }
  const resolvedPrefix = resolveGitWorkingDirectory(tokens, defaultCwd);
  if (resolvedPrefix === null) {
    return null;
  }

  const { gitCwd, gitIndex } = resolvedPrefix;
  if (tokens.length <= gitIndex) {
    return null;
  }
  if (tokens[gitIndex] !== "git") {
    return null;
  }

  const gitArguments = tokens.slice(gitIndex + 1);
  if (gitArguments.length === 0) {
    return null;
  }
  if (gitArguments.includes(SHELL_AND_OPERATOR)) {
    return null;
  }
  if (gitArguments[0].startsWith("-")) {
    return null;
  }

  const normalizedSubcommand = gitArguments[0].toLowerCase();
  if (!GIT_REMOTE_SUBCOMMANDS.has(normalizedSubcommand)) {
    return null;
  }

  return {
    command: "git",
    cwd: gitCwd,
    gitArguments,
    subcommand: normalizedSubcommand,
  };
}

export function createGitAskpassBundle(accessToken) {
  const normalizedAccessToken = assertNonEmptyString(accessToken, "accessToken");
  const bundleDirPath = fs.mkdtempSync(path.join(os.tmpdir(), "yinshi-git-"));
  fs.chmodSync(bundleDirPath, 0o700);

  const tokenFilePath = path.join(bundleDirPath, "token");
  const askpassPath = path.join(bundleDirPath, "askpass.sh");
  fs.writeFileSync(tokenFilePath, normalizedAccessToken, { encoding: "utf-8", mode: 0o600 });
  fs.chmodSync(tokenFilePath, 0o600);
  fs.writeFileSync(
    askpassPath,
    buildGitAskpassScript(tokenFilePath),
    { encoding: "utf-8", mode: 0o700 },
  );
  fs.chmodSync(askpassPath, 0o700);

  return {
    askpassPath,
    bundleDirPath,
    cleanup() {
      fs.rmSync(bundleDirPath, { recursive: true, force: true });
    },
  };
}

function createGitExecutionEnvironment(baseEnv, askpassPath) {
  const normalizedAskpassPath = assertNonEmptyString(askpassPath, "askpassPath");
  const executionEnvironment = {
    ...process.env,
    ...(baseEnv || {}),
  };
  executionEnvironment.GCM_INTERACTIVE = "Never";
  executionEnvironment.GIT_ASKPASS = normalizedAskpassPath;
  executionEnvironment.GIT_CONFIG_NOSYSTEM = "1";
  executionEnvironment.GIT_CONFIG_GLOBAL = os.devNull;
  executionEnvironment.GIT_PAGER = "cat";
  executionEnvironment.GIT_TERMINAL_PROMPT = "0";
  return executionEnvironment;
}

function createGitCommandArguments(gitArguments) {
  if (!Array.isArray(gitArguments)) {
    throw new TypeError("gitArguments must be an array");
  }
  if (gitArguments.length === 0) {
    throw new Error("gitArguments must not be empty");
  }
  return [
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "credential.helper=",
    ...gitArguments,
  ];
}

function executeGitCommand(parsedGitCommand, execOptions, gitAuth) {
  if (!parsedGitCommand || typeof parsedGitCommand !== "object") {
    throw new TypeError("parsedGitCommand is required");
  }
  const normalizedGitAuth = normalizeGitAuth(gitAuth);
  if (normalizedGitAuth === null) {
    throw new Error("gitAuth is required for authenticated git execution");
  }
  const askpassBundle = createGitAskpassBundle(normalizedGitAuth.accessToken);
  const gitCommandArguments = createGitCommandArguments(parsedGitCommand.gitArguments);
  const gitEnvironment = createGitExecutionEnvironment(execOptions?.env, askpassBundle.askpassPath);

  return new Promise((resolve, reject) => {
    const gitChild = spawn(
      parsedGitCommand.command,
      gitCommandArguments,
      {
        cwd: parsedGitCommand.cwd,
        detached: true,
        env: gitEnvironment,
        stdio: ["ignore", "pipe", "pipe"],
      },
    );

    let timedOut = false;
    const timeoutSeconds = execOptions?.timeout;
    const timeoutHandle = timeoutSeconds && timeoutSeconds > 0
      ? setTimeout(() => {
        timedOut = true;
        gitChild.kill();
      }, timeoutSeconds * 1000)
      : null;

    const onAbort = () => {
      gitChild.kill();
    };

    gitChild.stdout?.on("data", execOptions.onData);
    gitChild.stderr?.on("data", execOptions.onData);

    if (execOptions?.signal) {
      if (execOptions.signal.aborted) {
        onAbort();
      } else {
        execOptions.signal.addEventListener("abort", onAbort, { once: true });
      }
    }

    const cleanup = () => {
      if (timeoutHandle) {
        clearTimeout(timeoutHandle);
      }
      if (execOptions?.signal) {
        execOptions.signal.removeEventListener("abort", onAbort);
      }
      askpassBundle.cleanup();
    };

    gitChild.on("error", (error) => {
      cleanup();
      reject(error);
    });
    gitChild.on("close", (exitCode) => {
      cleanup();
      if (execOptions?.signal?.aborted) {
        reject(new Error("aborted"));
        return;
      }
      if (timedOut) {
        reject(new Error(`timeout:${timeoutSeconds}`));
        return;
      }
      resolve({ exitCode });
    });
  });
}

export function createGitRuntimeBashOperations(defaultCwd, gitAuth) {
  const normalizedDefaultCwd = assertNonEmptyString(defaultCwd, "defaultCwd");
  const normalizedGitAuth = normalizeGitAuth(gitAuth);
  const localBashOperations = createLocalBashOperations();

  return {
    exec(command, cwd, execOptions) {
      const effectiveCwd = assertNonEmptyString(cwd || normalizedDefaultCwd, "cwd");
      if (normalizedGitAuth === null) {
        return localBashOperations.exec(command, effectiveCwd, execOptions);
      }

      const parsedGitCommand = parseGitCommandForRuntimeAuth(command, effectiveCwd);
      if (parsedGitCommand === null) {
        return localBashOperations.exec(command, effectiveCwd, execOptions);
      }

      return executeGitCommand(parsedGitCommand, execOptions, normalizedGitAuth);
    },
  };
}

export function createGitAwareBashTool(cwd, gitAuth) {
  const normalizedCwd = assertNonEmptyString(cwd, "cwd");
  return createBashTool(normalizedCwd, {
    operations: createGitRuntimeBashOperations(normalizedCwd, gitAuth),
  });
}
