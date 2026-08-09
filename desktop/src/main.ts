import { createHash, randomUUID } from "node:crypto";
import { accessSync, constants } from "node:fs";
import { chmod, mkdir, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import {
  app,
  BrowserWindow,
  dialog,
  ipcMain,
  net,
  safeStorage,
  session,
  shell,
  type IpcMainInvokeEvent,
} from "electron";
import updaterPackage from "electron-updater";

import { resumeDesktopAccount } from "./accountSession.js";
import { DesktopAppController } from "./appController.js";
import {
  startAutomaticUpdates,
  type AutomaticUpdateController,
} from "./automaticUpdates.js";
import { startAuthCallbackListener } from "./authCallbackListener.js";
import { DesktopCredentialStore } from "./credentialStore.js";
import { detectFileVaultStatus } from "./diskEncryption.js";
import { DESKTOP_IPC_CHANNELS, type HostedApiRequest } from "./desktopApi.js";
import { bootstrapHelperSession } from "./helperBootstrap.js";
import { startManagedHelper, type ManagedHelper } from "./helperSupervisor.js";
import { HostedAccessSession } from "./hostedAccessSession.js";
import { HostedApiGateway } from "./hostedApiGateway.js";
import { startHostedSignIn, type HostedSignInStage } from "./hostedAuth.js";
import { startLocalRuntime } from "./localRuntime.js";
import {
  cloneRepositoryIntoProfile,
  DirtyRepositoryError,
} from "./localRepositoryImport.js";
import {
  buildRuntimeLaunchConfig,
  type RuntimeLaunchConfig,
} from "./runtimeLaunchConfig.js";
import { RuntimeSecretStore } from "./runtimeSecrets.js";
import { createBrowserWindowOptions } from "./security.js";
import { createShellPolicy } from "./shellPolicy.js";
import { startSidecar } from "./sidecarSupervisor.js";

const { autoUpdater } = updaterPackage;

const HOSTED_API_BASE_URL = "https://yinshi.io";
const EXTERNAL_ORIGINS = Object.freeze([
  "https://yinshi.io",
  "https://docs.yinshi.io",
  "https://github.com",
  "https://docs.github.com",
]);

const moduleDirectory = path.dirname(fileURLToPath(import.meta.url));
let mainWindow: BrowserWindow | undefined;
let controller: DesktopAppController | undefined;
let applicationOrigin: string | undefined;
let updateController: AutomaticUpdateController | undefined;
let quitting = false;

function shellEnvironment(): Record<string, string | undefined> {
  const user = os.userInfo();
  return {
    HOME: process.env.HOME ?? user.homedir,
    LANG: process.env.LANG ?? "en_US.UTF-8",
    LC_ALL: process.env.LC_ALL,
    LC_CTYPE: process.env.LC_CTYPE,
    LOGNAME: process.env.LOGNAME ?? user.username,
    PATH:
      process.env.PATH ??
      "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
    SHELL: process.env.SHELL ?? user.shell ?? "/bin/zsh",
    SSH_AUTH_SOCK: process.env.SSH_AUTH_SOCK,
    TMPDIR: process.env.TMPDIR ?? os.tmpdir(),
    USER: process.env.USER ?? user.username,
  };
}

function profileDirectoryPath(userId: string): string {
  if (typeof userId !== "string" || userId.length === 0) {
    throw new TypeError("desktop profile user ID is invalid");
  }
  const profileId = createHash("sha256").update(userId, "utf8").digest("hex");
  return path.join(app.getPath("userData"), "profiles", profileId);
}

async function ensureProfileDirectories(directoryPath: string): Promise<void> {
  const directories = [
    directoryPath,
    path.join(directoryPath, "backups"),
    path.join(directoryPath, "data"),
    path.join(directoryPath, "repositories"),
    path.join(directoryPath, "run"),
  ];
  for (const target of directories) {
    await mkdir(target, { mode: 0o700, recursive: true });
    await chmod(target, 0o700);
  }
}

function developmentExecutablePath(executableName: string): string {
  if (!/^[A-Za-z0-9._-]+$/.test(executableName)) {
    throw new TypeError("development executable name is invalid");
  }
  const searchPath = shellEnvironment().PATH;
  if (searchPath === undefined) {
    throw new Error("development Node search path is unavailable");
  }
  for (const directoryPath of searchPath.split(path.delimiter)) {
    if (!path.isAbsolute(directoryPath)) {
      continue;
    }
    const candidate = path.join(directoryPath, executableName);
    try {
      accessSync(candidate, constants.X_OK);
      return candidate;
    } catch {
      continue;
    }
  }
  throw new Error(`development ${executableName} executable is unavailable`);
}

function developmentLaunchConfig(
  packagedConfig: RuntimeLaunchConfig,
  profileDirectory: string,
): RuntimeLaunchConfig {
  if (app.isPackaged) {
    return packagedConfig;
  }
  const projectRoot = path.resolve(app.getAppPath(), "..");
  return {
    helper: {
      ...packagedConfig.helper,
      command: path.join(projectRoot, "backend", ".venv", "bin", "python"),
      workingDirectory: profileDirectory,
      args: [
        "-m",
        "yinshi.desktop_runtime",
        "--ready-fd",
        "3",
        "--asset-dir",
        path.join(projectRoot, "frontend", "dist"),
      ],
    },
    sidecar: {
      ...packagedConfig.sidecar,
      command: developmentExecutablePath("node"),
      workingDirectory: profileDirectory,
      args: [path.join(projectRoot, "sidecar", "src", "index.js")],
      environment: {
        ...packagedConfig.sidecar.environment,
        NODE_ENV: "development",
      },
    },
  };
}

function gitExecutablePath(): string {
  const gitCommand = app.isPackaged
    ? path.join(process.resourcesPath, "bin", "git")
    : developmentExecutablePath("git");
  accessSync(gitCommand, constants.X_OK);
  return gitCommand;
}

function signInFilePath(): string {
  if (app.isPackaged) {
    return path.join(app.getAppPath(), "assets", "signin.html");
  }
  return path.resolve(moduleDirectory, "..", "assets", "signin.html");
}

function currentShellPolicy() {
  return createShellPolicy({
    applicationOrigin,
    signInUrl: pathToFileURL(signInFilePath()).href,
    externalOrigins: EXTERNAL_ORIGINS,
  });
}

function installWindowSecurity(window: BrowserWindow): void {
  window.webContents.on("will-navigate", (event, url) => {
    if (!currentShellPolicy().navigationAllowed(url)) {
      event.preventDefault();
    }
  });
  window.webContents.on("will-redirect", (event, url) => {
    if (!currentShellPolicy().navigationAllowed(url)) {
      event.preventDefault();
    }
  });
  window.webContents.on("will-attach-webview", (event) => {
    event.preventDefault();
  });
  window.webContents.setWindowOpenHandler(({ url }) => {
    if (currentShellPolicy().externalAllowed(url)) {
      void shell.openExternal(url, { activate: true }).catch(() => undefined);
    }
    return { action: "deny" };
  });
}

function createMainWindow(): BrowserWindow {
  const preloadPath = path.join(moduleDirectory, "preload.cjs");
  const window = new BrowserWindow({
    ...createBrowserWindowOptions(preloadPath),
    width: 1_080,
    height: 760,
    minWidth: 760,
    minHeight: 560,
    titleBarStyle: "hiddenInset",
    backgroundColor: "#f4f1e9",
  });
  installWindowSecurity(window);
  window.on("closed", () => {
    if (mainWindow === window) {
      mainWindow = undefined;
    }
  });
  return window;
}

function requireWindow(): BrowserWindow {
  if (mainWindow === undefined || mainWindow.isDestroyed()) {
    throw new Error("desktop window is unavailable");
  }
  return mainWindow;
}

function requestFromAllowedPage(
  event: IpcMainInvokeEvent,
  channel: string,
): boolean {
  const sourceUrl = event.senderFrame?.url;
  if (sourceUrl === undefined) {
    return false;
  }
  const fromSignInPage = sourceUrl === pathToFileURL(signInFilePath()).href;
  if (channel === DESKTOP_IPC_CHANNELS.signIn) {
    return fromSignInPage;
  }
  if (
    fromSignInPage &&
    (channel === DESKTOP_IPC_CHANNELS.listProfiles ||
      channel === DESKTOP_IPC_CHANNELS.switchProfile)
  ) {
    return true;
  }
  if (applicationOrigin === undefined) {
    return false;
  }
  try {
    return new URL(sourceUrl).origin === applicationOrigin;
  } catch {
    return false;
  }
}

async function configureApplication(): Promise<DesktopAppController> {
  const secureDirectory = path.join(app.getPath("userData"), "secure");
  const credentialStore = new DesktopCredentialStore({
    directoryPath: secureDirectory,
    safeStorage,
  });
  const runtimeSecretStore = new RuntimeSecretStore({
    directoryPath: secureDirectory,
    safeStorage,
  });
  const fetchAdapter = (
    input: string | URL,
    init?: RequestInit,
  ): Promise<Response> => net.fetch(input.toString(), init);
  const electronSessionFetch = (
    input: string | URL,
    init?: RequestInit,
  ): Promise<Response> => session.defaultSession.fetch(input.toString(), init);

  const hostedAccessSession = new HostedAccessSession({
    resume: () =>
      resumeDesktopAccount({
        apiBaseUrl: HOSTED_API_BASE_URL,
        fetch: fetchAdapter,
        credentialStore,
      }),
  });
  const hostedApiGateway = new HostedApiGateway({
    apiBaseUrl: HOSTED_API_BASE_URL,
    fetch: fetchAdapter,
    getAccessToken: () =>
      hostedAccessSession.getAccessToken(Math.floor(Date.now() / 1_000)),
  });

  const appController = new DesktopAppController({
    resumeAccount: () => hostedAccessSession.resumeAccount(),
    signIn: async () => {
      let stage: HostedSignInStage | "starting-listener" = "starting-listener";
      let listener;
      try {
        listener = await startAuthCallbackListener({
          timeoutMs: 5 * 60 * 1_000,
        });
      } catch (error) {
        const errorName = error instanceof Error ? error.name : "UnknownError";
        console.error(`Desktop sign-in failed during ${stage}: ${errorName}`);
        throw new Error("Desktop sign-in failed");
      }
      try {
        const account = await startHostedSignIn({
          apiBaseUrl: HOSTED_API_BASE_URL,
          callbackUri: listener.callbackUri,
          deviceName: `Mac (${os.hostname().split(".", 1)[0] || "Yinshi"})`,
          fetch: fetchAdapter,
          openExternal: async (url) => {
            await shell.openExternal(url, { activate: true });
          },
          waitForCallback: async () => listener.waitForCallback(),
          credentialStore,
          onProgress: (nextStage) => {
            stage = nextStage;
            console.info(`Desktop sign-in entered ${nextStage}`);
          },
        });
        hostedAccessSession.setAccount({ mode: "online", ...account });
        return account;
      } catch {
        console.error(`Desktop sign-in failed during ${stage}`);
        throw new Error("Desktop sign-in failed");
      } finally {
        await listener.close();
      }
    },
    clearCredentials: async () => {
      hostedAccessSession.clear();
      await credentialStore.clear();
    },
    startHelper: async (profile): Promise<ManagedHelper> => {
      console.info("Desktop local runtime preparing profile");
      const profileDirectory = profileDirectoryPath(profile.user.id);
      await ensureProfileDirectories(profileDirectory);
      console.info("Desktop local runtime loading secrets");
      const runtimeSecrets = await runtimeSecretStore.loadOrCreate();
      console.info("Desktop local runtime building launch configuration");
      const packagedConfig = buildRuntimeLaunchConfig({
        resourcesPath: process.resourcesPath,
        profileDirectoryPath: profileDirectory,
        socketDirectoryPath: path.join(app.getPath("userData"), "run"),
        runtimeSecrets,
        shellEnvironment: shellEnvironment(),
      });
      const launchConfig = developmentLaunchConfig(
        packagedConfig,
        profileDirectory,
      );
      const socketPath = launchConfig.helper.environment.SIDECAR_SOCKET_PATH;
      if (socketPath === undefined) {
        throw new Error("desktop sidecar socket path is unavailable");
      }
      console.info("Desktop local runtime starting child processes");
      return startLocalRuntime({
        helper: launchConfig.helper,
        sidecar: launchConfig.sidecar,
        socketPath,
        startSidecar,
        startHelper: startManagedHelper,
      });
    },
    bootstrapHelper: async (helper) =>
      bootstrapHelperSession({
        ready: helper.ready,
        fetch: electronSessionFetch,
      }),
    showSignIn: async () => {
      applicationOrigin = undefined;
      const window = requireWindow();
      await window.loadFile(signInFilePath());
      if (!window.isVisible()) {
        window.show();
      }
    },
    loadApplication: async (origin) => {
      applicationOrigin = origin;
      const window = requireWindow();
      await window.loadURL(`${origin}/app`);
      if (!window.isVisible()) {
        window.show();
      }
    },
  });

  ipcMain.handle(DESKTOP_IPC_CHANNELS.signIn, async (event) => {
    if (!requestFromAllowedPage(event, DESKTOP_IPC_CHANNELS.signIn)) {
      throw new Error("Desktop IPC request denied");
    }
    try {
      await appController.signIn();
    } catch {
      throw new Error("Desktop sign-in failed");
    }
  });
  ipcMain.handle(DESKTOP_IPC_CHANNELS.signOut, async (event) => {
    if (!requestFromAllowedPage(event, DESKTOP_IPC_CHANNELS.signOut)) {
      throw new Error("Desktop IPC request denied");
    }
    try {
      await appController.signOut();
    } catch {
      throw new Error("Desktop sign-out failed");
    }
  });
  ipcMain.handle(
    DESKTOP_IPC_CHANNELS.hostedRequest,
    async (event, request: HostedApiRequest) => {
      if (!requestFromAllowedPage(event, DESKTOP_IPC_CHANNELS.hostedRequest)) {
        throw new Error("Desktop IPC request denied");
      }
      try {
        return await hostedApiGateway.request(request);
      } catch {
        throw new Error("Hosted API request failed");
      }
    },
  );
  ipcMain.handle(DESKTOP_IPC_CHANNELS.fileVaultStatus, async (event) => {
    if (!requestFromAllowedPage(event, DESKTOP_IPC_CHANNELS.fileVaultStatus)) {
      throw new Error("Desktop IPC request denied");
    }
    return detectFileVaultStatus();
  });
  ipcMain.handle(DESKTOP_IPC_CHANNELS.listProfiles, async (event) => {
    if (!requestFromAllowedPage(event, DESKTOP_IPC_CHANNELS.listProfiles)) {
      throw new Error("Desktop IPC request denied");
    }
    return credentialStore.list();
  });
  ipcMain.handle(
    DESKTOP_IPC_CHANNELS.switchProfile,
    async (event, userId: string) => {
      if (!requestFromAllowedPage(event, DESKTOP_IPC_CHANNELS.switchProfile)) {
        throw new Error("Desktop IPC request denied");
      }
      if (typeof userId !== "string" || !userId || userId.length > 256) {
        throw new Error("Desktop profile ID is invalid");
      }
      const previousProfile = await credentialStore.load();
      const selectedProfile = await credentialStore.select(userId);
      if (selectedProfile === null) {
        throw new Error("Desktop profile requires sign-in");
      }
      try {
        await appController.switchProfile();
      } catch {
        if (previousProfile === null) {
          throw new Error("Desktop profile switch failed");
        }
        await credentialStore.select(previousProfile.user.id);
        try {
          await appController.switchProfile();
        } catch {
          throw new Error(
            "Desktop profile switch failed and the previous profile could not restart",
          );
        }
        throw new Error("Desktop profile switch failed");
      }
    },
  );
  ipcMain.handle(
    DESKTOP_IPC_CHANNELS.removeProfile,
    async (event, userId: string) => {
      if (!requestFromAllowedPage(event, DESKTOP_IPC_CHANNELS.removeProfile)) {
        throw new Error("Desktop IPC request denied");
      }
      if (typeof userId !== "string" || !userId || userId.length > 256) {
        throw new Error("Desktop profile ID is invalid");
      }
      const profile = (await credentialStore.list()).find(
        (candidate) => candidate.user.id === userId,
      );
      if (profile === undefined) return;
      if (profile.active) {
        await appController.signOut();
      }
      await rm(profileDirectoryPath(userId), { force: true, recursive: true });
      await credentialStore.remove(userId);
    },
  );
  ipcMain.handle(DESKTOP_IPC_CHANNELS.importLocalRepository, async (event) => {
    if (
      !requestFromAllowedPage(event, DESKTOP_IPC_CHANNELS.importLocalRepository)
    ) {
      throw new Error("Desktop IPC request denied");
    }
    const profile = hostedAccessSession.profile;
    const origin = applicationOrigin;
    if (profile === undefined || origin === undefined) {
      throw new Error("Desktop account session is unavailable");
    }
    const window = requireWindow();
    const selection = await dialog.showOpenDialog(window, {
      title: "Import a local Git repository",
      buttonLabel: "Choose Repository",
      properties: ["openDirectory", "dontAddToRecent"],
    });
    const sourcePath = selection.filePaths[0];
    if (selection.canceled || sourcePath === undefined) {
      return { status: "cancelled" } as const;
    }

    const repositoryBasePath = path.join(
      profileDirectoryPath(profile.user.id),
      "repositories",
    );
    const destinationPath = path.join(repositoryBasePath, randomUUID());
    let clonedRepository;
    try {
      clonedRepository = await cloneRepositoryIntoProfile({
        gitCommand: gitExecutablePath(),
        sourcePath,
        managedBasePath: repositoryBasePath,
        destinationPath,
        allowDirty: false,
      });
    } catch (error) {
      if (!(error instanceof DirtyRepositoryError)) {
        throw new Error("Local repository import failed");
      }
      const confirmation = await dialog.showMessageBox(window, {
        type: "warning",
        title: "Repository has uncommitted changes",
        message: "Import committed state only?",
        detail:
          "Yinshi will leave the selected checkout untouched. Uncommitted and untracked files will not be copied.",
        buttons: ["Import Committed State", "Cancel"],
        defaultId: 1,
        cancelId: 1,
        noLink: true,
      });
      if (confirmation.response !== 0) {
        return { status: "cancelled" } as const;
      }
      try {
        clonedRepository = await cloneRepositoryIntoProfile({
          gitCommand: gitExecutablePath(),
          sourcePath,
          managedBasePath: repositoryBasePath,
          destinationPath,
          allowDirty: true,
        });
      } catch {
        throw new Error("Local repository import failed");
      }
    }

    try {
      const response = await electronSessionFetch(`${origin}/api/repos`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Requested-With": "XMLHttpRequest",
        },
        body: JSON.stringify({
          name: clonedRepository.name,
          local_path: clonedRepository.path,
        }),
        redirect: "error",
        signal: AbortSignal.timeout(30_000),
      });
      if (response.status !== 201) {
        throw new Error("Local repository registration failed");
      }
      const value: unknown = await response.json();
      if (typeof value !== "object" || value === null || Array.isArray(value)) {
        throw new Error("Local repository registration response is invalid");
      }
      const repository = value as Record<string, unknown>;
      if (
        typeof repository.id !== "string" ||
        typeof repository.name !== "string"
      ) {
        throw new Error("Local repository registration response is invalid");
      }
      return {
        status: "imported",
        repository: { id: repository.id, name: repository.name },
      } as const;
    } catch {
      await rm(clonedRepository.path, { recursive: true, force: true });
      throw new Error("Local repository import failed");
    }
  });
  return appController;
}

async function startApplication(): Promise<void> {
  session.defaultSession.setPermissionCheckHandler(() => false);
  session.defaultSession.setPermissionRequestHandler(
    (_webContents, _permission, callback) => {
      callback(false);
    },
  );
  session.defaultSession.on("will-download", (event) => {
    event.preventDefault();
  });
  mainWindow = createMainWindow();
  controller = await configureApplication();
  await controller.start();
  updateController = startAutomaticUpdates({
    isPackaged: app.isPackaged,
    updater: autoUpdater,
    schedule: (delayMs, callback) => {
      const timer = setTimeout(callback, delayMs);
      timer.unref();
      return { cancel: () => clearTimeout(timer) };
    },
    onDownloaded: () => {
      const window = mainWindow;
      if (window === undefined || window.isDestroyed()) {
        return;
      }
      void dialog
        .showMessageBox(window, {
          type: "info",
          title: "Yinshi update ready",
          message: "A signed Yinshi update is ready to install.",
          detail: "Restart now, or install automatically when you quit Yinshi.",
          buttons: ["Restart now", "Later"],
          defaultId: 0,
          cancelId: 1,
          noLink: true,
        })
        .then((result) => {
          if (result.response === 0) {
            autoUpdater.quitAndInstall(false, true);
          }
        })
        .catch(() => undefined);
    },
  });
}

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", () => {
    const window = mainWindow;
    if (window !== undefined && !window.isDestroyed()) {
      if (window.isMinimized()) {
        window.restore();
      }
      window.show();
      window.focus();
    }
  });

  app.on("activate", () => {
    if (mainWindow === undefined) {
      mainWindow = createMainWindow();
      if (applicationOrigin === undefined) {
        void mainWindow.loadFile(signInFilePath());
      } else {
        void mainWindow.loadURL(`${applicationOrigin}/`);
      }
    }
  });

  app.on("window-all-closed", () => {
    if (process.platform !== "darwin") {
      app.quit();
    }
  });

  app.on("before-quit", (event) => {
    if (quitting) {
      return;
    }
    event.preventDefault();
    quitting = true;
    void (async () => {
      try {
        updateController?.stop();
        await controller?.stop();
      } finally {
        app.quit();
      }
    })();
  });

  void app
    .whenReady()
    .then(startApplication)
    .catch(() => {
      dialog.showErrorBox(
        "Yinshi could not start",
        "The desktop runtime could not be started safely. No local workspace was opened.",
      );
      app.quit();
    });
}
