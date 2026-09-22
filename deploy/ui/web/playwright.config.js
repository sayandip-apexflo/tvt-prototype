import {defineConfig} from '@playwright/test';

export default defineConfig({
  testDir:'./tests',
  testMatch:'**/*.spec.js',
  outputDir:'/tmp/tvt-playwright-results',
  fullyParallel:false,
  use:{baseURL:'http://127.0.0.1:4174',channel:'chrome'},
  webServer:{
    command:'npm run dev -- --host 127.0.0.1 --port 4174',
    cwd:new URL('.',import.meta.url).pathname,
    url:'http://127.0.0.1:4174/dashboard',
    reuseExistingServer:true,
  },
});
