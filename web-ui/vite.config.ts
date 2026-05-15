import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Vite dev server proxies /api and /api/ws to the Python backend so the
// frontend can use relative URLs in both dev and prod.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8787",
        changeOrigin: true,
        ws: true,
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
  },
});
