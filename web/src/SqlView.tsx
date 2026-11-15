import { useEffect, useState } from "react";
import { api, type QueryResult } from "./api";
import { DataTable, ErrorNote } from "./components";

/** Every part is quoted so unusual names still parse. */
export const quoted = (name: string) => name.split(".").map((p) => `"${p.replace(/"/g, '""')}"`).join(".");

export function SqlView() {
  const [sql, setSql] = useState("");
  const [tables, setTables] = useState<string[]>([]);
  useEffect(() => {
    api.tables().then((t) => setTables(t.map((x) => x.name))).catch(() => setTables([]));
  }, []);
  const [result, setResult] = useState<QueryResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);

  const run = async () => {
    setRunning(true);
    setError(null);
    try {
      setResult(await api.query(sql));
    } catch (e) {
      setResult(null);
      setError((e as Error).message);
    } finally {
      setRunning(false);
    }
  };

  return (
    <div>
      <h2>SQL</h2>
      <p className="muted">Read-only. Refer to tables as <code>namespace.table</code>. Press Ctrl+Enter to run.</p>
      {tables.length > 0 && (
        <div className="chips" aria-label="Tables">
          {tables.map((t) => (
            <button key={t} className="chip" onClick={() => setSql(`select * from ${quoted(t)} limit 20`)}>
              {t}
            </button>
          ))}
        </div>
      )}
      <label htmlFor="sql" className="sr-only">
        SQL query
      </label>
      <textarea
        id="sql"
        value={sql}
        spellCheck={false}
        placeholder="select * from namespace.table limit 10"
        rows={6}
        onChange={(e) => setSql(e.target.value)}
        onKeyDown={(e) => {
          if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
            e.preventDefault();
            void run();
          }
        }}
      />
      <div className="row">
        <button className="primary" onClick={() => void run()} disabled={running || !sql.trim()}>
          Run
        </button>
        {result && (
          <span role="status" className="muted">
            {result.rows.length} row{result.rows.length === 1 ? "" : "s"}
            {result.truncated ? " (more rows exist; showing the first batch)" : ""}
          </span>
        )}
      </div>
      <ErrorNote error={error} />
      {result && <DataTable caption="Query result" columns={result.columns} rows={result.rows} />}
    </div>
  );
}
