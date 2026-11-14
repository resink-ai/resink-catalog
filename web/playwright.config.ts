import { defineConfig } from "@playwright/test";

// One end-to-end smoke test against the real `resink-catalog serve`, with no network after install (D10).
// It drives the Chrome already on the machine (channel "chrome"), so no browser is downloaded. Set
// E2E_CHANNEL=chromium to use a Playwright-managed browser instead (`npx playwright install chromium`).
const PORT = 8791;
const DIR = "/tmp/resink-e2e";

export default defineConfig({
  testDir: "e2e",
  timeout: 60_000,
  reporter: "list",
  use: { baseURL: `http://127.0.0.1:${PORT}`, channel: process.env.E2E_CHANNEL ?? "chrome", trace: "retain-on-failure" },
  webServer: {
    // fresh data directory with a known token and the 50-file sample set
    command:
      `rm -rf ${DIR} && mkdir -p ${DIR}/home && echo e2e-token > ${DIR}/home/token && ` +
      `uv run python tests/samples.py ${DIR}/exports && ` +
      `uv run resink-catalog serve --port ${PORT} --no-browser --home ${DIR}/home`,
    cwd: "..",
    url: `http://127.0.0.1:${PORT}/healthz`,
    reuseExistingServer: false,
    timeout: 60_000,
  },
});
