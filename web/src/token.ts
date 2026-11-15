// The access token arrives in the URL fragment (`/#token=...`), which browsers never send to any server. It is kept
// in memory and in sessionStorage (one tab, one origin including the port, so no other local service can read it),
// removed from the address bar, and sent as `Authorization: Bearer` on every API call. There is no cookie.

let current: string | null = null;

export function takeTokenFromUrl(loc: Location = location, hist: History = history): void {
  const m = /(?:^#|&)token=([^&]+)/.exec(loc.hash);
  if (m) {
    current = decodeURIComponent(m[1]);
    try {
      sessionStorage.setItem("rc_token", current);
    } catch {
      /* storage blocked: the token stays in memory for this page load */
    }
    hist.replaceState(null, "", loc.pathname + loc.search);
  }
}

export function getToken(): string | null {
  if (current) return current;
  try {
    return sessionStorage.getItem("rc_token");
  } catch {
    return null;
  }
}

export function resetToken(): void {
  current = null;
}
