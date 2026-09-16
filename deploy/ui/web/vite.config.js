import {defineConfig} from 'vite';

// Both dashboards are served below Ingress path prefixes. Relative assets keep
// JavaScript and CSS on the same routed prefix instead of requesting /assets.
export default defineConfig({base: './'});
