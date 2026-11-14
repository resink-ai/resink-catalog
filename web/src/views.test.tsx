import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "./App";
import { SourceReview } from "./FoldersView";
import { SqlView } from "./SqlView";
import { TableDetail } from "./TablesView";

type Route = (url: string, init?: RequestInit) => unknown;
function routes(map: Record<string, Route | unknown>) {
  const fn = vi.fn(async (url: string, init?: RequestInit) => {
    const key = `${init?.method ?? "GET"} ${url.split("?")[0]}`;
    if (!(key in map)) return { ok: false, status: 404, statusText: "nf", json: async () => ({ detail: `no route ${key}` }) };
    const v = map[key];
    const body = typeof v === "function" ? (v as Route)(url, init) : v;
    return { ok: true, status: 200, statusText: "OK", json: async () => body };
  });
  vi.stubGlobal("fetch", fn);
  return fn;
}
afterEach(() => vi.unstubAllGlobals());

const info = {
  name: "shop.orders",
  push_ready: true,
  tags: [],
  notes: [],
  push_blockers: [],
  location: "x",
  schema: [
    { name: "id", type: "long", required: false },
    { name: "name", type: "string", required: false },
  ],
  snapshots: [
    { snapshot_id: 1, timestamp_ms: 1700000000000, operation: "append", added_records: 3, deleted_records: 0, total_records: 3 },
    { snapshot_id: 2, timestamp_ms: 1700000100000, operation: "append", added_records: 1, deleted_records: 0, total_records: 4 },
  ],
};

test("table detail shows schema, sample rows and snapshot history", async () => {
  routes({
    "GET /api/v1/tables/shop.orders": info,
    "GET /api/v1/tables/shop.orders/sample": { columns: ["id", "name"], rows: [[1, "ann"], [2, null]] },
  });
  render(<TableDetail name="shop.orders" />);
  expect(await screen.findByText("ready to push")).toBeInTheDocument();
  expect(screen.getByRole("columnheader", { name: "type" })).toBeInTheDocument();
  expect(screen.getByRole("cell", { name: "long" })).toBeInTheDocument();
  await userEvent.click(screen.getByRole("tab", { name: "Sample rows" }));
  expect(await screen.findByRole("cell", { name: "ann" })).toBeInTheDocument();
  await userEvent.click(screen.getByRole("tab", { name: "Snapshot history" }));
  expect(screen.getAllByRole("row")).toHaveLength(3); // header + two snapshots
});

test("a table that cannot be pushed says why", async () => {
  routes({ "GET /api/v1/tables/t": { ...info, push_ready: false, push_blockers: ["delete files"] } });
  render(<TableDetail name="t" />);
  expect(await screen.findByText(/not pushable: delete files/)).toBeInTheDocument();
});

test("SQL view runs a query with the button and with Ctrl+Enter, and shows errors", async () => {
  const f = routes({
    "GET /api/v1/tables": [{ name: "shop.orders", columns: [], snapshots: 1, rows: 4, tags: [] }],
    "POST /api/v1/query": (_u: string, init?: RequestInit) =>
      JSON.parse(String(init?.body)).sql.includes("bad")
        ? undefined
        : { columns: ["n"], rows: [[4]], truncated: false },
  });
  render(<SqlView />);
  await userEvent.click(await screen.findByRole("button", { name: "shop.orders" }));
  expect(screen.getByLabelText("SQL query")).toHaveValue('select * from "shop"."orders" limit 20');
  await userEvent.click(screen.getByRole("button", { name: "Run" }));
  expect(await screen.findByRole("cell", { name: "4" })).toBeInTheDocument();
  await userEvent.clear(screen.getByLabelText("SQL query"));
  await userEvent.type(screen.getByLabelText("SQL query"), "select 1{Control>}{Enter}{/Control}");
  await waitFor(() => expect(f.mock.calls.filter((c) => String(c[0]).includes("/query")).length).toBeGreaterThanOrEqual(2));
});

test("SQL errors are shown, not thrown", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) =>
      String(url).includes("/query")
        ? { ok: false, status: 400, statusText: "bad", json: async () => ({ detail: "only read-only SELECT statements are allowed" }) }
        : { ok: true, status: 200, statusText: "OK", json: async () => [] },
    ),
  );
  render(<SqlView />);
  await userEvent.type(screen.getByLabelText("SQL query"), "drop table x");
  await userEvent.click(screen.getByRole("button", { name: "Run" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(/read-only/);
});

test("changing a column type saves it, refreshes the preview and imports with the change", async () => {
  const source = { id: 7, path: "/d/s.csv", format: "csv", target_table: "d.s", mode: "overwrite", status: "proposed", error: null, rows: null, column_types: {} };
  const preview = (type: string) => ({
    source,
    columns: [
      { name: "id", inferred: "long", type },
      { name: "name", inferred: "string", type: "string" },
    ],
    rows: [{ id: type === "string" ? "1" : 1, name: "ann" }],
    total_rows: 1,
    error: null,
  });
  let current = "long";
  const f = routes({
    "GET /api/v1/sources/7/preview": () => preview(current),
    "POST /api/v1/sources/7/types": (_u: string, init?: RequestInit) => {
      current = JSON.parse(String(init?.body)).column_types.id ?? "long";
      return source;
    },
    "POST /api/v1/sources/import": [{ source_id: 7, path: "/d/s.csv", table: "d.s", outcome: "created", rows: 1, error: null }],
  });
  const done = vi.fn();
  render(<SourceReview source={source} onDone={done} />);
  const select = await screen.findByLabelText("Type of id");
  expect(select).toHaveValue("long");
  await userEvent.selectOptions(select, "string");
  await waitFor(() => expect(screen.getByLabelText("Type of id")).toHaveValue("string"));
  expect(screen.getByText("inferred long")).toBeInTheDocument();
  expect(await screen.findByRole("cell", { name: "1" })).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Import this file" }));
  await waitFor(() => expect(done).toHaveBeenCalled());
  const importCall = f.mock.calls.find((c) => String(c[0]).endsWith("/sources/import"))!;
  expect(JSON.parse(String((importCall[1] as RequestInit).body))).toEqual({ source_id: 7, column_types: { id: "string" } });
});

test("the theme button cycles and sets the attribute on the page", async () => {
  routes({ "GET /api/v1/tables": [] });
  localStorage.clear();
  render(<App />);
  const btn = screen.getByRole("button", { name: /Theme: system/ });
  await userEvent.click(btn);
  expect(document.documentElement.getAttribute("data-theme")).toBe("light");
  await userEvent.click(screen.getByRole("button", { name: /Theme: light/ }));
  expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
  await userEvent.click(screen.getByRole("button", { name: /Theme: dark/ }));
  expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
});

test("navigation links switch between the three sections", async () => {
  routes({ "GET /api/v1/tables": [], "GET /api/v1/roots": [], "GET /api/v1/sources": [] });
  render(<App />);
  await userEvent.click(screen.getByRole("link", { name: "Folders" }));
  expect(await screen.findByRole("heading", { name: "Folders" })).toBeInTheDocument();
  await userEvent.click(screen.getByRole("link", { name: "SQL" }));
  expect(await screen.findByRole("heading", { name: "SQL" })).toBeInTheDocument();
});

test("what the AI wrote is marked ai, what the user wrote is marked you", async () => {
  routes({
    "GET /api/v1/tables/shop.orders": {
      ...info,
      tags: [
        { tag: "sales", source: "ai" },
        { tag: "finance", source: "user" },
      ],
      notes: [
        { id: 1, target: "shop.orders", body: "One row per order.", author: "ai", created_at: "2026-11-14T10:00:00+00:00" },
        { id: 2, target: "shop.orders.id", body: "Order number.", author: "user", created_at: "2026-11-14T11:00:00+00:00" },
      ],
    },
  });
  render(<TableDetail name="shop.orders" />);
  await userEvent.click(await screen.findByRole("tab", { name: /Notes and tags \(4\)/ }));
  const ai = screen.getByText("One row per order.").closest("li")!;
  expect(ai).toHaveClass("ai");
  expect(ai.querySelector('[aria-label="written by ai"]')).not.toBeNull();
  const mine = screen.getByText("Order number.").closest("li")!;
  expect(mine).toHaveClass("user");
  expect(mine.querySelector('[aria-label="written by you"]')).not.toBeNull();
  expect(mine).toHaveTextContent("id"); // the column the note is about
  const tags = screen.getByRole("list", { name: "Tags (editable)" });
  expect(tags.querySelector(".tag.ai")).toHaveTextContent("sales");
  expect(tags.querySelector(".tag.user")).toHaveTextContent("finance");
});

test("adding a note and a tag, and deleting a note, call the API and reload", async () => {
  let notes: unknown[] = [];
  const f = routes({
    "GET /api/v1/tables/shop.orders": () => ({ ...info, tags: [], notes }),
    "POST /api/v1/tables/shop.orders/notes": (_u: string, init?: RequestInit) => {
      notes = [{ id: 9, target: JSON.parse(String(init?.body)).target, body: JSON.parse(String(init?.body)).body, author: "user", created_at: "2026-11-14T10:00:00+00:00" }];
      return notes[0];
    },
    "POST /api/v1/tables/shop.orders/tags": [],
    "DELETE /api/v1/notes/9": () => {
      notes = [];
      return { removed: 9 };
    },
  });
  render(<TableDetail name="shop.orders" />);
  await userEvent.click(await screen.findByRole("tab", { name: /Notes and tags/ }));
  await userEvent.selectOptions(screen.getByLabelText("About"), "shop.orders.id");
  await userEvent.type(screen.getByLabelText("Note"), "Order number");
  await userEvent.click(screen.getByRole("button", { name: "Add note" }));
  expect(await screen.findByText("Order number")).toBeInTheDocument();
  const post = f.mock.calls.find((c) => String(c[0]).endsWith("/notes"))!;
  expect(JSON.parse(String((post[1] as RequestInit).body))).toEqual({ body: "Order number", target: "shop.orders.id" });
  await userEvent.type(screen.getByLabelText("Add tags (comma separated)"), "sales, finance");
  await userEvent.click(screen.getByRole("button", { name: "Add" }));
  await waitFor(() => expect(f.mock.calls.some((c) => String(c[0]).endsWith("/tags"))).toBe(true));
  await userEvent.click(screen.getByRole("button", { name: "Delete note 9" }));
  expect(await screen.findByText("No notes yet.")).toBeInTheDocument();
});
