export const DESKTOP_IPC_CHANNELS = Object.freeze({
  signIn: "desktop:account:sign-in",
  signOut: "desktop:account:sign-out",
  importLocalRepository: "desktop:repository:import-local",
});

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
}

declare global {
  interface Window {
    readonly yinshiDesktop: YinshiDesktopApi;
  }
}
