import { defineConfig } from 'vite';
import { resolve } from 'path';

// Vite bundle servi par Flask via /static/dist/
// PR4 modularisera ~5000 lignes de JS inline depuis app/templates/index.html
// en ES modules importés depuis frontend/shell.js.
// Build-id : 2026-05-24-2 — force buildx à invalider la layer vite cache
// (sinon les changements de tabs/*.js ne ressortaient pas dans shell.js).
export default defineConfig({
  root: 'frontend',
  server: {
    port: 5173,
    strictPort: true,
    cors: true,
    origin: 'http://localhost:5173',
    hmr: { host: 'localhost', protocol: 'ws', port: 5173 },
  },
  build: {
    outDir: '../app/static/dist',
    emptyOutDir: true,
    rollupOptions: {
      input: {
        shell: resolve(__dirname, 'frontend/shell.js'),
      },
      output: {
        entryFileNames: '[name].js',
        chunkFileNames: '[name]-[hash].js',
        assetFileNames: '[name]-[hash].[ext]',
      },
    },
  },
});
