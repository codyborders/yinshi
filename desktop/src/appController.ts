import {
  DesktopSignInRequiredError,
  type DesktopAccountSession,
} from "./accountSession.js";
import type { DesktopCredentialProfile } from "./credentialStore.js";
import type { ManagedHelper } from "./helperSupervisor.js";
import type { HostedDesktopSession } from "./hostedAuth.js";

export interface DesktopAppControllerDependencies {
  resumeAccount(): Promise<DesktopAccountSession>;
  signIn(): Promise<HostedDesktopSession>;
  clearCredentials(): Promise<void>;
  startHelper(profile: DesktopCredentialProfile): Promise<ManagedHelper>;
  bootstrapHelper(helper: ManagedHelper): Promise<string>;
  showSignIn(): Promise<void>;
  loadApplication(origin: string): Promise<void>;
}

function assertDependencies(dependencies: DesktopAppControllerDependencies): void {
  const methods: Array<keyof DesktopAppControllerDependencies> = [
    "resumeAccount",
    "signIn",
    "clearCredentials",
    "startHelper",
    "bootstrapHelper",
    "showSignIn",
    "loadApplication",
  ];
  for (const method of methods) {
    if (typeof dependencies[method] !== "function") {
      throw new TypeError(`desktop app controller ${method} dependency is invalid`);
    }
  }
}

export class DesktopAppController {
  readonly #dependencies: DesktopAppControllerDependencies;
  #helper: ManagedHelper | undefined;
  #operation: Promise<void> = Promise.resolve();
  #started = false;
  #stopped = false;

  constructor(dependencies: DesktopAppControllerDependencies) {
    assertDependencies(dependencies);
    this.#dependencies = dependencies;
  }

  #enqueue(operation: () => Promise<void>): Promise<void> {
    const result = this.#operation.then(operation);
    this.#operation = result.catch(() => undefined);
    return result;
  }

  async #stopHelper(): Promise<void> {
    const helper = this.#helper;
    this.#helper = undefined;
    if (helper !== undefined) {
      await helper.stop();
    }
  }

  async #loadProfile(profile: DesktopCredentialProfile): Promise<void> {
    await this.#stopHelper();
    const helper = await this.#dependencies.startHelper(profile);
    if (!helper.running || helper.processId < 1) {
      await helper.stop();
      throw new Error("desktop helper failed to start");
    }
    this.#helper = helper;
    try {
      const origin = await this.#dependencies.bootstrapHelper(helper);
      await this.#dependencies.loadApplication(origin);
    } catch (error) {
      await this.#stopHelper();
      throw error;
    }
  }

  start(): Promise<void> {
    return this.#enqueue(async () => {
      if (this.#stopped) {
        throw new Error("desktop app controller is stopped");
      }
      if (this.#started) {
        return;
      }
      this.#started = true;
      let session: DesktopAccountSession;
      try {
        session = await this.#dependencies.resumeAccount();
      } catch (error) {
        if (error instanceof DesktopSignInRequiredError) {
          await this.#dependencies.showSignIn();
          return;
        }
        throw error;
      }
      if (session.mode === "signed-out") {
        await this.#dependencies.showSignIn();
        return;
      }
      await this.#loadProfile(session.profile);
    });
  }

  signIn(): Promise<void> {
    return this.#enqueue(async () => {
      if (!this.#started || this.#stopped) {
        throw new Error("desktop app controller is not active");
      }
      const session = await this.#dependencies.signIn();
      await this.#loadProfile(session.profile);
    });
  }

  switchProfile(): Promise<void> {
    return this.#enqueue(async () => {
      if (!this.#started || this.#stopped) {
        throw new Error("desktop app controller is not active");
      }
      const session = await this.#dependencies.resumeAccount();
      if (session.mode === "signed-out") {
        throw new DesktopSignInRequiredError();
      }
      await this.#loadProfile(session.profile);
    });
  }

  signOut(): Promise<void> {
    return this.#enqueue(async () => {
      if (!this.#started || this.#stopped) {
        throw new Error("desktop app controller is not active");
      }
      await this.#stopHelper();
      await this.#dependencies.clearCredentials();
      await this.#dependencies.showSignIn();
    });
  }

  stop(): Promise<void> {
    return this.#enqueue(async () => {
      if (this.#stopped) {
        return;
      }
      this.#stopped = true;
      await this.#stopHelper();
    });
  }
}
