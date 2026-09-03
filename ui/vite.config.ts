import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import packageJson from "./package.json";

const tvtVersion = packageJson.version;

export default defineConfig({
  plugins: [react()],
  define: { __TVT_VERSION__: JSON.stringify(tvtVersion) },
  build: { outDir: "../tvt_edge/static", emptyOutDir: true },
  server: {
    host: "127.0.0.1",
    proxy: { "/api": "http://127.0.0.1:8088" },
  },
  test: { environment: "jsdom", setupFiles: "./src/test.setup.ts" },
});
