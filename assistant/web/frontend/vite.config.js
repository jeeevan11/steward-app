import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Localhost only. The dev server proxies /api to the FastAPI backend so the
// browser only ever talks to 127.0.0.1.
export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
});
