// Thin client for the local API. The access token travels in an HttpOnly cookie set when the browser opened the
// link printed by `resink-catalog serve`, so nothing here ever touches it.

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

export type Author = "user" | "ai";
export type Tag = { tag: string; source: Author };
export type Note = { id: number; target: string; body: string; author: Author; created_at: string };
export type TableSummary = { name: string; columns: string[]; snapshots: number; rows: number; tags: Tag[] };
export type Snapshot = {
  snapshot_id: number;
  timestamp_ms: number;
  operation: string;
  added_records: number;
  deleted_records: number;
  total_records: number;
};
export type TableInfo = {
  name: string;
  tags: Tag[];
  notes: Note[];
  push_ready: boolean;
  push_blockers: string[];
  schema: { name: string; type: string; required: boolean }[];
  snapshots: Snapshot[];
  location: string;
};
export type Sample = { columns: string[]; rows: unknown[][] };
export type Root = { id: number; path: string; name: string; last_scan: string | null; counts: Record<string, number> };
export type Source = {
  id: number;
  path: string;
  format: string;
  target_table: string;
  mode: string;
  status: string;
  error: string | null;
  rows: number | null;
  column_types: Record<string, string>;
};
export type PreviewColumn = { name: string; inferred: string; type: string };
export type Preview = {
  source: Source;
  columns: PreviewColumn[];
  rows: Record<string, unknown>[];
  total_rows?: number;
  error: string | null;
};
export type ImportResult = { source_id: number; path: string; table: string; outcome: string; rows: number | null; error: string | null };
export type QueryResult = { columns: string[]; rows: unknown[][]; truncated: boolean };

export const TYPE_NAMES = ["string", "long", "double", "boolean", "date", "timestamp"] as const;

async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(path, {
    method,
    credentials: "same-origin",
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const data = await res.json();
      detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail ?? data);
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, res.status === 401 ? "Not signed in: open the link printed by `resink-catalog serve`." : detail);
  }
  return (await res.json()) as T;
}

const enc = encodeURIComponent;

export const api = {
  tables: () => call<TableSummary[]>("GET", "/api/v1/tables"),
  table: (name: string) => call<TableInfo>("GET", `/api/v1/tables/${enc(name)}`),
  sample: (name: string) => call<Sample>("GET", `/api/v1/tables/${enc(name)}/sample?limit=20`),
  addTags: (name: string, tags: string[]) => call<unknown>("POST", `/api/v1/tables/${enc(name)}/tags`, { tags }),
  removeTag: (name: string, tag: string) => call<unknown>("DELETE", `/api/v1/tables/${enc(name)}/tags/${enc(tag)}`),
  addNote: (name: string, body: string, target?: string) =>
    call<Note>("POST", `/api/v1/tables/${enc(name)}/notes`, { body, target }),
  removeNote: (id: number) => call<unknown>("DELETE", `/api/v1/notes/${id}`),
  roots: () => call<Root[]>("GET", "/api/v1/roots"),
  sources: () => call<Source[]>("GET", "/api/v1/sources"),
  addFolder: (path: string) => call<Source[]>("POST", "/api/v1/roots", { path }),
  preview: (id: number) => call<Preview>("GET", `/api/v1/sources/${id}/preview`),
  setTypes: (id: number, column_types: Record<string, string>) =>
    call<Source>("POST", `/api/v1/sources/${id}/types`, { column_types }),
  importSource: (id: number, column_types?: Record<string, string>) =>
    call<ImportResult[]>("POST", "/api/v1/sources/import", { source_id: id, column_types }),
  importFolder: (root: string) => call<ImportResult[]>("POST", "/api/v1/sources/import", { root }),
  scan: () => call("POST", "/api/v1/sources/scan"),
  query: (sql: string) => call<QueryResult>("POST", "/api/v1/query", { sql }),
};

/** Only the choices that differ from what was inferred need to be sent (and remembered). */
export function changedTypes(columns: PreviewColumn[]): Record<string, string> {
  return Object.fromEntries(columns.filter((c) => c.type !== c.inferred).map((c) => [c.name, c.type]));
}

export function formatCell(v: unknown): string {
  if (v === null || v === undefined) return "";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}
