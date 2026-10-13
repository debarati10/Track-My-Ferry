import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// `npm run dev` serves the map on :5173 and proxies the API (run `trackmyferry run` on :8000).
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": "http://localhost:8000",
      "/ws": { target: "ws://localhost:8000", ws: true },
    },
  },
  build: { outDir: "dist", sourcemap: true },
});
