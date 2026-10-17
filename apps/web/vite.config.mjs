import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";
import { healthBody } from "./health.mjs";
function health(server) {
  server.middlewares.use("/health", (_req, res) => {
    res.setHeader("content-type", "application/json; charset=utf-8");
    res.end(healthBody());
  });
}
export default defineConfig({
  root: fileURLToPath(new URL(".", import.meta.url)),
  plugins: [
    react(),
    {
      name: "focusmate-health",
      configureServer: health,
      configurePreviewServer: health,
    },
  ],
  server: {
    host: "127.0.0.1",
    proxy: {
      "/api/v1": process.env.FOCUSMATE_API_URL || "http://127.0.0.1:8000",
    },
  },
  preview: {
    host: "127.0.0.1",
    proxy: {
      "/api/v1": process.env.FOCUSMATE_API_URL || "http://127.0.0.1:8000",
    },
  },
  build: { outDir: "../../dist/web", emptyOutDir: true },
});
