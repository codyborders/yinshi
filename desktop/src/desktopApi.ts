export const DESKTOP_IPC_CHANNELS = Object.freeze({
  signIn: "desktop:account:sign-in",
  signOut: "desktop:account:sign-out",
  importLocalRepository: "desktop:repository:import-local",
  hostedRequest: "desktop:hosted-api:request",
  fileVaultStatus: "desktop:security:file-vault-status",
  listProfiles: "desktop:profiles:list",
  switchProfile: "desktop:profiles:switch",
  removeProfile: "desktop:profiles:remove",
});

export interface HostedApiRequest {
  readonly method: "DELETE" | "GET" | "PATCH" | "POST" | "PUT";
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

export interface DesktopProfileSummary {
  readonly user: { readonly id: string; readonly email: string };
  readonly hasCredentials: boolean;
  readonly active: boolean;
}

export interface YinshiDesktopApi {
  signIn(): Promise<void>;
  signOut(): Promise<void>;
  importLocalRepository(): Promise<LocalRepositoryImportResult>;
  hostedRequest(request: HostedApiRequest): Promise<HostedApiResponse>;
  fileVaultStatus(): Promise<{ readonly status: "disabled" | "enabled" | "unknown" }>;
  listProfiles(): Promise<readonly DesktopProfileSummary[]>;
  switchProfile(userId: string): Promise<void>;
  removeProfile(userId: string): Promise<void>;
}

declare global {
  interface Window {
    readonly yinshiDesktop: YinshiDesktopApi;
  }
}
