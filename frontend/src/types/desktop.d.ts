interface DesktopHostedApiRequest {
  readonly method: "DELETE" | "GET" | "POST";
  readonly path: string;
  readonly body?: Readonly<Record<string, unknown>>;
}

interface DesktopHostedApiResponse {
  readonly status: number;
  readonly body: unknown;
}

interface YinshiDesktopBridge {
  hostedRequest(request: DesktopHostedApiRequest): Promise<DesktopHostedApiResponse>;
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
