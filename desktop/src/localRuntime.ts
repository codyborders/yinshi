import type {
  ManagedHelper,
  StartManagedHelperOptions,
} from "./helperSupervisor.js";
import type { ChildLaunchConfig } from "./runtimeLaunchConfig.js";
import type { ManagedSidecar, SidecarOptions } from "./sidecarSupervisor.js";

export interface StartLocalRuntimeOptions {
  readonly helper: ChildLaunchConfig;
  readonly sidecar: ChildLaunchConfig;
  readonly socketPath: string;
  readonly startSidecar: (options: SidecarOptions) => Promise<ManagedSidecar>;
  readonly startHelper: (options: StartManagedHelperOptions) => Promise<ManagedHelper>;
}

export async function startLocalRuntime(
  options: StartLocalRuntimeOptions,
): Promise<ManagedHelper> {
  if (options.helper.environment.SIDECAR_SOCKET_PATH !== options.socketPath) {
    throw new Error("helper sidecar socket path does not match runtime socket path");
  }
  if (options.sidecar.environment.SIDECAR_SOCKET_PATH !== options.socketPath) {
    throw new Error("sidecar socket path does not match runtime socket path");
  }
  const sidecar = await options.startSidecar({
    command: options.sidecar.command,
    args: options.sidecar.args,
    environment: options.sidecar.environment,
    workingDirectory: options.sidecar.workingDirectory,
    socketPath: options.socketPath,
    startupTimeoutMs: 15_000,
    shutdownTimeoutMs: 5_000,
  });

  let helper: ManagedHelper;
  try {
    helper = await options.startHelper({
      command: options.helper.command,
      arguments: [...options.helper.args],
      environment: { ...options.helper.environment },
      workingDirectory: options.helper.workingDirectory,
      readinessTimeoutMs: 15_000,
      shutdownTimeoutMs: 5_000,
    });
  } catch (startupError) {
    try {
      await sidecar.stop();
    } catch (cleanupError) {
      throw new AggregateError(
        [startupError, cleanupError],
        "Local helper startup and sidecar cleanup failed",
      );
    }
    throw startupError;
  }

  let stopOperation: Promise<void> | undefined;
  return {
    ready: helper.ready,
    processId: helper.processId,
    get running(): boolean {
      return helper.running && sidecar.running;
    },
    stop(): Promise<void> {
      stopOperation ??= (async () => {
        let helperError: unknown;
        try {
          await helper.stop();
        } catch (error) {
          helperError = error;
        }
        try {
          await sidecar.stop();
        } catch (sidecarError) {
          if (helperError !== undefined) {
            throw new AggregateError(
              [helperError, sidecarError],
              "Local helper and sidecar shutdown failed",
            );
          }
          throw sidecarError;
        }
        if (helperError !== undefined) {
          throw helperError;
        }
      })();
      return stopOperation;
    },
  };
}
