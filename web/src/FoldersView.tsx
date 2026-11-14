import { useCallback, useEffect, useState } from "react";
import { api, changedTypes, TYPE_NAMES, type ImportResult, type Preview, type Root, type Source } from "./api";
import { Badge, DataTable, ErrorNote, statusTone } from "./components";

export function FoldersView({ go }: { go: (to: string) => void }) {
  const [roots, setRoots] = useState<Root[]>([]);
  const [sources, setSources] = useState<Source[]>([]);
  const [path, setPath] = useState("");
  const [open, setOpen] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const [r, s] = await Promise.all([api.roots(), api.sources()]);
      setRoots(r);
      setSources(s);
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);
  useEffect(() => {
    void refresh();
  }, [refresh]);

  const summarize = (results: ImportResult[]) => {
    const failed = results.filter((r) => r.outcome === "error");
    setNotice(`${results.length - failed.length} imported, ${failed.length} failed.`);
    setError(failed.length ? failed.map((f) => `${f.path.split("/").pop()}: ${f.error}`).join("\n") : null);
  };

  const guard = async (fn: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await fn();
      await refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      <h2>Folders</h2>
      <form
        className="row"
        onSubmit={(e) => {
          e.preventDefault();
          if (path.trim()) void guard(async () => { const found = await api.addFolder(path.trim()); setNotice(`${found.length} files proposed. Nothing is imported until you confirm.`); setPath(""); });
        }}
      >
        <label htmlFor="folder-path">Folder on this computer</label>
        <input id="folder-path" value={path} onChange={(e) => setPath(e.target.value)} placeholder="/home/me/exports" />
        <button type="submit" disabled={busy || !path.trim()}>
          Propose
        </button>
      </form>
      {notice && <p role="status" className="notice">{notice}</p>}
      <ErrorNote error={error} />
      {roots.length === 0 && <p className="muted">No folders added yet.</p>}
      {roots.map((root) => {
        const mine = sources.filter((s) => s.path.startsWith(root.path));
        const pending = mine.filter((s) => ["proposed", "changed", "error"].includes(s.status)).length;
        return (
          <section key={root.id} aria-label={root.path} className="card">
            <header className="row between">
              <div>
                <strong>{root.name}</strong> <span className="muted">{root.path}</span>
                <div className="muted">{Object.entries(root.counts).map(([k, v]) => `${v} ${k}`).join(" · ")}</div>
              </div>
              <button disabled={busy || pending === 0} onClick={() => void guard(async () => summarize(await api.importFolder(root.path)))}>
                Import {pending} pending
              </button>
            </header>
            <div className="table-wrap">
              <table>
                <caption className="sr-only">Files in {root.name}</caption>
                <thead>
                  <tr>
                    <th scope="col">File</th>
                    <th scope="col">Table</th>
                    <th scope="col">Mode</th>
                    <th scope="col">Status</th>
                    <th scope="col"><span className="sr-only">Actions</span></th>
                  </tr>
                </thead>
                <tbody>
                  {mine.map((s) => (
                    <tr key={s.id}>
                      <td>{s.path.slice(root.path.length + 1)}</td>
                      <td>
                        {s.status === "imported" ? (
                          <a href={`/tables/${s.target_table}`} onClick={(e) => { e.preventDefault(); go(`/tables/${s.target_table}`); }}>{s.target_table}</a>
                        ) : (
                          s.target_table
                        )}
                      </td>
                      <td>{s.mode === "append" ? "append (repeated export)" : "replace"}</td>
                      <td>
                        <Badge tone={statusTone(s.status)}>{s.status}</Badge>
                        {s.error && <div className="muted small">{s.error}</div>}
                      </td>
                      <td>
                        <button onClick={() => setOpen(open === s.id ? null : s.id)} aria-expanded={open === s.id}>
                          {open === s.id ? "Close" : "Review"}
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {mine.filter((s) => s.id === open).map((s) => (
              <SourceReview key={s.id} source={s} onDone={(r) => { setOpen(null); summarize(r); void refresh(); }} />
            ))}
          </section>
        );
      })}
    </div>
  );
}

export function SourceReview({ source, onDone }: { source: Source; onDone: (r: ImportResult[]) => void }) {
  const [preview, setPreview] = useState<Preview | null>(null);
  const [types, setTypes] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.preview(source.id).then((p) => { setPreview(p); setTypes(Object.fromEntries(p.columns.map((c) => [c.name, c.type]))); }).catch((e) => setError(e.message));
  }, [source.id]);

  if (error) return <ErrorNote error={error} />;
  if (!preview) return <p className="muted">Reading the file…</p>;
  const columns = preview.columns.map((c) => ({ ...c, type: types[c.name] ?? c.type }));
  const rows = preview.rows.map((r) => preview.columns.map((c) => r[c.name]));
  const changes = changedTypes(columns);

  const doImport = async () => {
    setBusy(true);
    try {
      onDone(await api.importSource(source.id, changes));
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  };
  const refreshPreview = async (next: Record<string, string>) => {
    setTypes(next);
    try {
      await api.setTypes(source.id, changedTypes(preview.columns.map((c) => ({ ...c, type: next[c.name] ?? c.type }))));
      setPreview(await api.preview(source.id));
    } catch (e) {
      setError((e as Error).message);
    }
  };

  return (
    <div className="review" role="region" aria-label={`Review ${source.path.split("/").pop()}`}>
      <h3>Column types</h3>
      <p className="muted">{preview.total_rows ?? "?"} rows. The importer guessed these types; change any that are wrong.</p>
      <ErrorNote error={preview.error} />
      <div className="types">
        {columns.map((c) => (
          <label key={c.name}>
            <span>{c.name}</span>
            <select value={c.type} onChange={(e) => void refreshPreview({ ...types, [c.name]: e.target.value })} aria-label={`Type of ${c.name}`}>
              {TYPE_NAMES.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
            {c.type !== c.inferred && <span className="muted small">inferred {c.inferred}</span>}
          </label>
        ))}
      </div>
      <DataTable caption="Preview" columns={preview.columns.map((c) => c.name)} rows={rows} />
      <div className="row">
        <button className="primary" onClick={() => void doImport()} disabled={busy}>
          Import this file
        </button>
        {Object.keys(changes).length > 0 && <span className="muted">with {Object.keys(changes).length} changed type(s)</span>}
      </div>
    </div>
  );
}
