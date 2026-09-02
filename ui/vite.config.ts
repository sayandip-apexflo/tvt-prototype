import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  build: { outDir: "../tvt_edge/static", emptyOutDir: true },
  server: {
    host: "127.0.0.1",
    proxy: { "/api": "http://127.0.0.1:8088" },
  },
  test: { environment: "jsdom", setupFiles: "./src/test.setup.ts" },
});
