import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In dev, the React app is served by Vite (port 5173) and proxies /api to the
// FastAPI backend (ui/server.py) on 8100. In prod, `vite build` emits to
// dist/, which ui/server.py serves itself — one origin, no proxy.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8100",
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
