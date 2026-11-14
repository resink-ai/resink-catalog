/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The build goes straight into the Python package, which serves it (`serve` mounts src/resink_catalog/static).
export default defineConfig({
  plugins: [react()],
  build: { outDir: "../src/resink_catalog/static", emptyOutDir: true },
  server: { proxy: { "/api": "http://127.0.0.1:8765", "/iceberg": "http://127.0.0.1:8765" } },
  test: { environment: "jsdom", setupFiles: ["./src/test-setup.ts"], globals: true, exclude: ["e2e/**", "node_modules/**"] },
});
