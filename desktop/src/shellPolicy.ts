export interface ShellPolicyOptions {
  readonly applicationOrigin: string | undefined;
  readonly signInUrl: string;
  readonly externalOrigins: readonly string[];
}

export interface ShellPolicy {
  navigationAllowed(url: string): boolean;
  externalAllowed(url: string): boolean;
}

function parseApplicationOrigin(value: string | undefined): string | undefined {
  if (value === undefined) {
    return undefined;
  }
  const url = new URL(value);
  if (
    url.protocol !== "http:" ||
    url.hostname !== "127.0.0.1" ||
    url.port === "" ||
    url.pathname !== "/" ||
    url.search !== "" ||
    url.hash !== "" ||
    url.username !== "" ||
    url.password !== ""
  ) {
    throw new TypeError("applicationOrigin must be an exact loopback HTTP origin");
  }
  return url.origin;
}

export function createShellPolicy(options: ShellPolicyOptions): ShellPolicy {
  const signInUrl = new URL(options.signInUrl);
  if (
    signInUrl.protocol !== "file:" ||
    signInUrl.search !== "" ||
    signInUrl.hash !== "" ||
    signInUrl.username !== "" ||
    signInUrl.password !== ""
  ) {
    throw new TypeError("signInUrl must be an exact file URL");
  }
  const applicationOrigin = parseApplicationOrigin(options.applicationOrigin);
  const externalOrigins = new Set(
    options.externalOrigins.map((value) => {
      const url = new URL(value);
      if (
        url.protocol !== "https:" ||
        url.pathname !== "/" ||
        url.search !== "" ||
        url.hash !== "" ||
        url.username !== "" ||
        url.password !== ""
      ) {
        throw new TypeError("external origins must be exact HTTPS origins");
      }
      return url.origin;
    }),
  );

  return Object.freeze({
    navigationAllowed(value: string): boolean {
      let url: URL;
      try {
        url = new URL(value);
      } catch {
        return false;
      }
      if (url.protocol === "file:") {
        return url.href === signInUrl.href;
      }
      return applicationOrigin !== undefined && url.origin === applicationOrigin;
    },
    externalAllowed(value: string): boolean {
      let url: URL;
      try {
        url = new URL(value);
      } catch {
        return false;
      }
      return url.protocol === "https:" && externalOrigins.has(url.origin);
    },
  });
}
