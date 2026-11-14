import { useEffect, useState } from "react";
import { api, type Author, type Note, type Sample, type TableInfo, type TableSummary } from "./api";
import { Badge, DataTable, ErrorNote } from "./components";

type Tab = "schema" | "sample" | "snapshots" | "notes";

export function TablesView({ selected, go }: { selected: string | null; go: (to: string) => void }) {
  const [tables, setTables] = useState<TableSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.tables().then(setTables).catch((e) => setError(e.message));
  }, []);

  if (error) return <ErrorNote error={error} />;
  if (!tables) return <p className="muted">Loading…</p>;
  return (
    <div className="split">
      <section aria-label="Tables" className="list">
        <h2>Tables</h2>
        {tables.length === 0 && (
          <p className="muted">
            No tables yet. Add a folder of CSV or Parquet files under <a href="/folders" onClick={(e) => { e.preventDefault(); go("/folders"); }}>Folders</a>.
          </p>
        )}
        <ul>
          {tables.map((t) => (
            <li key={t.name}>
              <a
                href={`/tables/${t.name}`}
                aria-current={t.name === selected ? "page" : undefined}
                onClick={(e) => {
                  e.preventDefault();
                  go(`/tables/${t.name}`);
                }}
              >
                <strong>{t.name}</strong>
                {t.tags.length > 0 && <TagList tags={t.tags} />}
                <span className="muted">
                  {t.rows} rows · {t.snapshots} snapshot{t.snapshots === 1 ? "" : "s"}
                </span>
              </a>
            </li>
          ))}
        </ul>
      </section>
      <section aria-label="Table detail" className="detail">
        {selected ? <TableDetail key={selected} name={selected} /> : <p className="muted">Select a table.</p>}
      </section>
    </div>
  );
}

export function TableDetail({ name }: { name: string }) {
  const [tab, setTab] = useState<Tab>("schema");
  const [info, setInfo] = useState<TableInfo | null>(null);
  const [sample, setSample] = useState<Sample | null>(null);
  const [error, setError] = useState<string | null>(null);
  const reload = () => api.table(name).then(setInfo).catch((e) => setError(e.message));

  useEffect(() => {
    api.table(name).then(setInfo).catch((e) => setError(e.message));
  }, [name]);
  useEffect(() => {
    if (tab === "sample" && !sample) api.sample(name).then(setSample).catch((e) => setError(e.message));
  }, [tab, sample, name]);

  if (error) return <ErrorNote error={error} />;
  if (!info) return <p className="muted">Loading…</p>;
  return (
    <div>
      <h2>
        {info.name}{" "}
        {info.push_ready ? <Badge tone="ok">ready to push</Badge> : <Badge tone="warn">not pushable: {info.push_blockers.join(", ")}</Badge>}
      </h2>
      {info.tags.length > 0 && <TagList tags={info.tags} />}
      <div role="tablist" aria-label="Table views" className="tabs">
        {(["schema", "sample", "snapshots", "notes"] as Tab[]).map((t) => (
          <button key={t} role="tab" aria-selected={tab === t} onClick={() => setTab(t)}>
            {t === "schema" ? "Schema" : t === "sample" ? "Sample rows" : t === "snapshots" ? "Snapshot history" : `Notes and tags (${info.notes_total + info.tags.length})`}
          </button>
        ))}
      </div>
      <div role="tabpanel">
        {tab === "schema" && (
          <DataTable
            caption="Schema"
            columns={["column", "type", "required"]}
            rows={info.schema.map((f) => [f.name, f.type, f.required ? "yes" : "no"])}
          />
        )}
        {tab === "sample" && (sample ? <DataTable caption="Sample rows" columns={sample.columns} rows={sample.rows} /> : <p className="muted">Loading…</p>)}
        {tab === "snapshots" && (
          <DataTable
            caption="Snapshot history"
            columns={["when", "operation", "added", "deleted", "total rows", "snapshot id"]}
            rows={[...info.snapshots]
              .reverse()
              .map((s) => [new Date(s.timestamp_ms).toLocaleString(), s.operation, s.added_records, s.deleted_records, s.total_records, String(s.snapshot_id)])}
          />
        )}
        {tab === "notes" && <Annotations info={info} onChange={() => void reload()} />}
      </div>
    </div>
  );
}

/** Who wrote something: the AI's words are marked, so the user always knows what a person did not say. */
export function AuthorBadge({ author }: { author: Author }) {
  return author === "ai" ? (
    <Badge tone="info">
      <span aria-label="written by ai">ai</span>
    </Badge>
  ) : (
    <Badge tone="ok">
      <span aria-label="written by you">you</span>
    </Badge>
  );
}

export function TagList({ tags }: { tags: { tag: string; source: Author }[] }) {
  return (
    <ul className="tag-list" aria-label="Tags">
      {tags.map((t) => (
        <li key={t.tag} className={`tag ${t.source}`}>
          {t.tag}
          {t.source === "ai" && <span className="tag-ai" aria-label="written by ai">ai</span>}
        </li>
      ))}
    </ul>
  );
}

function Annotations({ info, onChange }: { info: TableInfo; onChange: () => void }) {
  const [tag, setTag] = useState("");
  const [note, setNote] = useState("");
  const [target, setTarget] = useState(info.name);
  const [error, setError] = useState<string | null>(null);
  const [more, setMore] = useState<{ notes: Note[]; next: number | null } | null>(null); // later pages
  useEffect(() => setMore(null), [info]); // a reload starts again from the first page
  const notes = [...info.notes, ...(more?.notes ?? [])];
  const next = more ? more.next : info.notes_next;
  const run = async (fn: () => Promise<unknown>) => {
    setError(null);
    try {
      await fn();
      onChange();
    } catch (e) {
      setError((e as Error).message);
    }
  };
  return (
    <div>
      <ErrorNote error={error} />
      <h3>Tags</h3>
      <ul className="tag-list" aria-label="Tags (editable)">
        {info.tags.map((t) => (
          <li key={t.id} className={`tag ${t.source}`}>
            {t.tag} <AuthorBadge author={t.source} />
            <button className="ghost small" aria-label={`Remove tag ${t.tag}`} onClick={() => void run(() => api.removeTag(t.id))}>
              ×
            </button>
          </li>
        ))}
      </ul>
      <form className="row" onSubmit={(e) => { e.preventDefault(); if (tag.trim()) void run(async () => { await api.addTags(info.name, tag.split(",")); setTag(""); }); }}>
        <label htmlFor="new-tag">Add tags (comma separated)</label>
        <input id="new-tag" value={tag} onChange={(e) => setTag(e.target.value)} />
        <button type="submit" disabled={!tag.trim()}>Add</button>
      </form>
      <h3>Notes</h3>
      {notes.length === 0 && <p className="muted">No notes yet.</p>}
      <ul className="notes">
        {notes.map((n) => (
          <li key={n.id} className={`note ${n.author}`}>
            <div className="row between">
              <span>
                <AuthorBadge author={n.author} /> <strong>{n.target === info.name ? "table" : n.target.slice(info.name.length + 1)}</strong>{" "}
                <span className="muted small">{new Date(n.created_at).toLocaleString()}</span>
              </span>
              <button className="ghost small" aria-label={`Delete note ${n.id}`} onClick={() => void run(() => api.removeNote(n.id))}>
                Delete
              </button>
            </div>
            <p>{n.body}</p>
          </li>
        ))}
      </ul>
      {next !== null && (
        <button
          className="ghost"
          onClick={() =>
            api
              .table(info.name, next)
              .then((page) => setMore({ notes: [...(more?.notes ?? []), ...page.notes], next: page.notes_next }))
              .catch((e) => setError((e as Error).message))
          }
        >
          Show more notes ({notes.length} of {info.notes_total})
        </button>
      )}
      <form onSubmit={(e) => { e.preventDefault(); if (note.trim()) void run(async () => { await api.addNote(info.name, note, target); setNote(""); }); }}>
        <div className="row">
          <label htmlFor="note-target">About</label>
          <select id="note-target" value={target} onChange={(e) => setTarget(e.target.value)}>
            <option value={info.name}>the table</option>
            {info.schema.map((f) => (
              <option key={f.name} value={`${info.name}.${f.name}`}>
                column {f.name}
              </option>
            ))}
          </select>
        </div>
        <label htmlFor="new-note" className="sr-only">Note</label>
        <textarea id="new-note" rows={3} value={note} onChange={(e) => setNote(e.target.value)} placeholder="What is this data? Units, quirks, how it is used." />
        <button type="submit" disabled={!note.trim()}>Add note</button>
      </form>
    </div>
  );
}
