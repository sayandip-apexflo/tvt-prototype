import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";
import packageJson from "./package.json";

const tvtVersion = packageJson.version;

// Vite emits <script type="module"> before <link rel="stylesheet"> in the built
// index.html. A script earlier in the document isn't blocked by a stylesheet that
// comes after it, so the app can render (forcing layout) before its CSS has loaded —
// a flash of unstyled content that Firefox devtools reports as a forced-layout warning.
// Moving the stylesheet link ahead of the script tag makes the script wait on it.
function reorderStylesheetsBeforeScripts(): Plugin {
  return {
    name: "reorder-stylesheets-before-scripts",
    transformIndexHtml: {
      order: "post",
      handler(html) {
        const stylesheetTags = html.match(/<link[^>]*rel="stylesheet"[^>]*>/g);
        if (!stylesheetTags?.length) return html;
        let output = html;
        for (const tag of stylesheetTags) output = output.replace(tag, "");
        return output.replace(/<script type="module"/, `${stylesheetTags.join("")}<script type="module"`);
      },
    },
  };
}

export default defineConfig({
  plugins: [react(), reorderStylesheetsBeforeScripts()],
  define: { __TVT_VERSION__: JSON.stringify(tvtVersion) },
  build: { outDir: "../tvt_edge/static", emptyOutDir: true },
  server: {
    host: "127.0.0.1",
    proxy: { "/api": "http://127.0.0.1:8089" },
  },
  test: { environment: "jsdom", setupFiles: "./src/test.setup.ts" },
});
