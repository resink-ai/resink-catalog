import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import "./styles.css";
import { applyTheme, loadTheme } from "./theme";

// The access token arrived in the link; the server turned it into a cookie. Keep it out of history and screenshots.
if (new URLSearchParams(location.search).has("token")) history.replaceState(null, "", location.pathname);
applyTheme(loadTheme());

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
