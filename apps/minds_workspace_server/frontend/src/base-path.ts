/**
 * Reads the application base path from a <meta> tag injected by the backend.
 * When running behind a reverse proxy with a path prefix (e.g. /myapp), the
 * backend sets <meta name="minds-workspace-server-base-path" content="/myapp"> so that
 * the frontend can build correct URLs and route prefixes.
 *
 * The returned value never has a trailing slash. For an app served at the
 * domain root, it returns "".
 */

let cachedBasePath: string | null = null;

export function getBasePath(): string {
  if (cachedBasePath !== null) {
    return cachedBasePath;
  }
  const metaElement = document.querySelector('meta[name="minds-workspace-server-base-path"]');
  const rawValue = metaElement?.getAttribute("content") ?? "";
  cachedBasePath = rawValue.replace(/\/+$/, "");
  return cachedBasePath;
}

export function apiUrl(path: string): string {
  return getBasePath() + path;
}

let cachedHostname: string | null = null;

export function getHostname(): string {
  if (cachedHostname !== null) {
    return cachedHostname;
  }
  const metaElement = document.querySelector('meta[name="minds-workspace-server-hostname"]');
  cachedHostname = metaElement?.getAttribute("content") ?? "localhost";
  return cachedHostname;
}
