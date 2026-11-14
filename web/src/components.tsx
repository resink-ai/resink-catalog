import type { ReactNode } from "react";
import { formatCell } from "./api";

export function DataTable({ columns, rows, caption }: { columns: string[]; rows: unknown[][]; caption?: string }) {
  if (columns.length === 0) return <p className="muted">No columns.</p>;
  return (
    <div className="table-wrap">
      <table>
        {caption && <caption className="sr-only">{caption}</caption>}
        <thead>
          <tr>
            {columns.map((c) => (
              <th key={c} scope="col">
                {c}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i}>
              {r.map((v, j) => (
                <td key={j} className={typeof v === "number" ? "num" : undefined}>
                  {formatCell(v)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {rows.length === 0 && <p className="muted pad">No rows.</p>}
    </div>
  );
}

export function ErrorNote({ error }: { error: string | null }) {
  return error ? (
    <p role="alert" className="error">
      {error}
    </p>
  ) : null;
}

export function Badge({ tone, children }: { tone: "ok" | "warn" | "bad" | "info"; children: ReactNode }) {
  return <span className={`badge ${tone}`}>{children}</span>;
}

export const statusTone = (s: string): "ok" | "warn" | "bad" | "info" =>
  s === "imported" ? "ok" : s === "error" || s === "missing" ? "bad" : s === "changed" ? "warn" : "info";
