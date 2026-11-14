import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import "./styles.css";
import { takeTokenFromUrl } from "./token";
import { applyTheme, loadTheme } from "./theme";

takeTokenFromUrl(); // the token came in the link's #fragment; keep it out of history and screenshots
applyTheme(loadTheme());

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
