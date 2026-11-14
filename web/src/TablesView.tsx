import { useEffect, useState } from "react";
import { api, type Sample, type TableInfo, type TableSummary } from "./api";
import { Badge, DataTable, ErrorNote } from "./components";

type Tab = "schema" | "sample" | "snapshots";

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
      <div role="tablist" aria-label="Table views" className="tabs">
        {(["schema", "sample", "snapshots"] as Tab[]).map((t) => (
          <button key={t} role="tab" aria-selected={tab === t} onClick={() => setTab(t)}>
            {t === "schema" ? "Schema" : t === "sample" ? "Sample rows" : "Snapshot history"}
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
      </div>
    </div>
  );
}
