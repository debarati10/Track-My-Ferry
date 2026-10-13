const BASE: string = import.meta.env.VITE_API_BASE ?? "";

export async function getJSON<T>(path: string, signal?: AbortSignal): Promise<T> {
  // no-cache: always check with the server first, so an update shows up without clearing the browser cache
  const res = await fetch(`${BASE}${path}`, { signal, cache: "no-cache", headers: { Accept: "application/json" } });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} for ${path}`);
  return (await res.json()) as T;
}

export function liveSocketUrl(): string {
  const explicit: string | undefined = import.meta.env.VITE_WS_URL;
  if (explicit) return explicit;
  if (BASE) return BASE.replace(/^http/, "ws") + "/ws/live";
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${window.location.host}/ws/live`;
}
