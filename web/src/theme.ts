export type Theme = "system" | "light" | "dark";
const KEY = "rc-theme";

export function loadTheme(): Theme {
  try {
    const v = localStorage.getItem(KEY);
    if (v === "light" || v === "dark" || v === "system") return v;
  } catch {
    /* storage can be blocked */
  }
  return "system";
}

export function applyTheme(theme: Theme): void {
  const root = document.documentElement;
  if (theme === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", theme);
  try {
    localStorage.setItem(KEY, theme);
  } catch {
    /* the choice just does not persist */
  }
}

export const nextTheme = (t: Theme): Theme => (t === "system" ? "light" : t === "light" ? "dark" : "system");
