interface YinshiDesktopBridge {
  importLocalRepository(): Promise<
    | { readonly status: "cancelled" }
    | {
        readonly status: "imported";
        readonly repository: { readonly id: string; readonly name: string };
      }
  >;
  signOut(): Promise<void>;
}

interface Window {
  readonly yinshiDesktop?: YinshiDesktopBridge;
}
