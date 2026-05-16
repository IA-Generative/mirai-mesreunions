import { defineConfig } from 'vite';
import { resolve } from 'path';

// Vite bundle servi par Flask via /static/dist/
// PR4 modularisera ~5000 lignes de JS inline depuis app/templates/index.html
// en ES modules importés depuis frontend/shell.js.
export default defineConfig({
  root: 'frontend',
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
