import { api, ApiError, changedTypes, formatCell } from "./api";
import { getToken, resetToken, takeTokenFromUrl } from "./token";

function mockFetch(status: number, body: unknown) {
  const fn = vi.fn().mockResolvedValue({ ok: status < 400, status, statusText: "x", json: async () => body });
  vi.stubGlobal("fetch", fn);
  return fn;
}
afterEach(() => vi.unstubAllGlobals());

test("only changed column types are sent", () => {
  expect(
    changedTypes([
      { name: "a", inferred: "long", type: "long" },
      { name: "b", inferred: "long", type: "string" },
    ]),
  ).toEqual({ b: "string" });
});

test("cells render nulls as empty and objects as JSON", () => {
  expect(formatCell(null)).toBe("");
  expect(formatCell(3)).toBe("3");
  expect(formatCell({ a: 1 })).toBe('{"a":1}');
});

test("requests carry the bearer token and a JSON body, and no credentials mode", async () => {
  sessionStorage.setItem("rc_token", "abc");
  resetToken();
  const f = mockFetch(200, { columns: [], rows: [], truncated: false });
  await api.query("select 1");
  const [url, init] = f.mock.calls[0];
  expect(url).toBe("/api/v1/query");
  expect(init.headers.Authorization).toBe("Bearer abc");
  expect(init.credentials).toBeUndefined();
  expect(JSON.parse(init.body)).toEqual({ sql: "select 1" });
});

test("a 401 explains how to sign in", async () => {
  mockFetch(401, { detail: "missing or invalid access token" });
  await expect(api.tables()).rejects.toThrow(/open the link printed by/i);
});

test("server messages reach the user", async () => {
  mockFetch(400, { detail: "only read-only SELECT statements are allowed" });
  const err = await api.query("drop table x").catch((e) => e);
  expect(err).toBeInstanceOf(ApiError);
  expect(err.message).toMatch(/read-only/);
});

test("table names are URL-encoded", async () => {
  const f = mockFetch(200, {});
  await api.table("a b.c");
  expect(f.mock.calls[0][0]).toBe("/api/v1/tables/a%20b.c");
});

test("the token in the URL fragment is kept per tab, removed from the address bar, and never put in a cookie", () => {
  sessionStorage.clear();
  resetToken();
  const replaceState = vi.fn();
  takeTokenFromUrl({ hash: "#token=a%2Fb-c", pathname: "/tables", search: "?x=1" } as Location, { replaceState } as unknown as History);
  expect(getToken()).toBe("a/b-c");
  expect(replaceState).toHaveBeenCalledWith(null, "", "/tables?x=1");
  expect(sessionStorage.getItem("rc_token")).toBe("a/b-c");
  expect(document.cookie).toBe("");
  resetToken(); // a reload: the fragment is gone, sessionStorage still has it
  expect(getToken()).toBe("a/b-c");
});
