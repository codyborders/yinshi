if (
  window.location.pathname === "/app" ||
  window.location.pathname.startsWith("/app/")
) {
  void import("./runner/encryptedRunnerClient")
    .then(({ preloadEncryptedRunnerCrypto }) => preloadEncryptedRunnerCrypto())
    .catch(() => undefined);
}

void import("./main");

export {};
