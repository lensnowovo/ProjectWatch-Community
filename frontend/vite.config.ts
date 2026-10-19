import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

// Build straight into the Python package so the wheel ships the UI.
export default defineConfig(({ mode }) => {
  // Development-only proxy target; never expose settings or API keys to the browser.
  const env = loadEnv(mode, ".", "PW_");
  const apiTarget = env.PW_API_TARGET || "http://127.0.0.1:8796";
  return {
  plugins: [react()],
  base: "/",
  build: {
    outDir: "../src/project_watch/web",
    emptyOutDir: true,
    assetsDir: "assets",
    // Stable names: rebuilding overwrites files in place instead of leaving stale hashed copies.
    rollupOptions: {
      output: {
        entryFileNames: "assets/app.js",
        chunkFileNames: "assets/[name].js",
        assetFileNames: "assets/[name][extname]",
      },
    },
  },
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: { "/api": { target: apiTarget, changeOrigin: false } },
  },
  };
});
