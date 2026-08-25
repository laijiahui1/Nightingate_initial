import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Vite config for the Nightingale SPA.
//
// `host: true` binds all interfaces so the container can serve the dev server.
// The `/api` and `/ws` proxies point at the `api` service on the compose
// network; when running Vite on the host (outside docker) change the target to
// `http://localhost:8000`.
export default defineConfig({
  plugins: [react()],
  server: {
    host: true,
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://api:8000',
        changeOrigin: true,
      },
      '/ws': {
        target: 'ws://api:8000',
        ws: true,
      },
    },
  },
});
