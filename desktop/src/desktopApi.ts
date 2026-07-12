export const DESKTOP_IPC_CHANNELS = Object.freeze({
  signIn: "desktop:account:sign-in",
  signOut: "desktop:account:sign-out",
  importLocalRepository: "desktop:repository:import-local",
  hostedRequest: "desktop:hosted-api:request",
});

export interface HostedApiRequest {
  readonly method: "DELETE" | "GET" | "POST";
  readonly path: string;
  readonly body?: Readonly<Record<string, unknown>>;
}

export interface HostedApiResponse {
  readonly status: number;
  readonly body: unknown;
}

export type LocalRepositoryImportResult =
  | { readonly status: "cancelled" }
  | {
      readonly status: "imported";
      readonly repository: { readonly id: string; readonly name: string };
    };

export interface YinshiDesktopApi {
  signIn(): Promise<void>;
  signOut(): Promise<void>;
  importLocalRepository(): Promise<LocalRepositoryImportResult>;
  hostedRequest(request: HostedApiRequest): Promise<HostedApiResponse>;
}

declare global {
  interface Window {
    readonly yinshiDesktop: YinshiDesktopApi;
  }
}
