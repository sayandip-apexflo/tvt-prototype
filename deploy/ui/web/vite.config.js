import {defineConfig} from 'vite';

// Vite emits <script type="module"> before <link rel="stylesheet"> in the built
// index.html. A script earlier in the document isn't blocked by a stylesheet that
// comes after it, so the app can render (forcing layout) before its CSS has loaded —
// a flash of unstyled content that Firefox devtools reports as a forced-layout warning.
// Moving the stylesheet link ahead of the script tag makes the script wait on it.
function reorderStylesheetsBeforeScripts() {
  return {
    name: 'reorder-stylesheets-before-scripts',
    transformIndexHtml: {
      order: 'post',
      handler(html) {
        const stylesheetTags = html.match(/<link[^>]*rel="stylesheet"[^>]*>/g);
        if (!stylesheetTags?.length) return html;
        let output = html;
        for (const tag of stylesheetTags) output = output.replace(tag, '');
        return output.replace(/<script type="module"/, `${stylesheetTags.join('')}<script type="module"`);
      },
    },
  };
}

// Both dashboards are served below Ingress path prefixes. Relative assets keep
// JavaScript and CSS on the same routed prefix instead of requesting /assets.
export default defineConfig({base: './', plugins: [reorderStylesheetsBeforeScripts()]});
