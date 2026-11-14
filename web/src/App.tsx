import { useEffect, useState } from "react";
import { FoldersView } from "./FoldersView";
import { SqlView } from "./SqlView";
import { TablesView } from "./TablesView";
import { applyTheme, loadTheme, nextTheme, type Theme } from "./theme";
import { usePath } from "./useRoute";

const THEME_LABEL: Record<Theme, string> = { system: "Theme: system", light: "Theme: light", dark: "Theme: dark" };

export function App() {
  const [path, go] = usePath();
  const [theme, setTheme] = useState<Theme>(loadTheme);
  useEffect(() => applyTheme(theme), [theme]);

  const section = path.startsWith("/folders") ? "folders" : path.startsWith("/sql") ? "sql" : "tables";
  const selected = path.startsWith("/tables/") ? decodeURIComponent(path.slice("/tables/".length)) : null;
  const link = (to: string, label: string, key: string) => (
    <a href={to} aria-current={section === key ? "page" : undefined} onClick={(e) => { e.preventDefault(); go(to); }}>
      {label}
    </a>
  );

  return (
    <>
      <header className="top">
        <strong className="brand">Resink Catalog</strong>
        <nav aria-label="Main">
          {link("/tables", "Tables", "tables")}
          {link("/folders", "Folders", "folders")}
          {link("/sql", "SQL", "sql")}
        </nav>
        <button className="ghost" onClick={() => setTheme(nextTheme(theme))} aria-label={`${THEME_LABEL[theme]}. Click to change.`}>
          {THEME_LABEL[theme]}
        </button>
      </header>
      <main>
        {section === "tables" && <TablesView selected={selected} go={go} />}
        {section === "folders" && <FoldersView go={go} />}
        {section === "sql" && <SqlView />}
      </main>
    </>
  );
}
