import { defineConfig } from 'vitest/config';
import solid from 'vite-plugin-solid';
import { resolve } from 'node:path';

export default defineConfig({
  plugins: [solid({ ssr: false })],
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: [resolve(__dirname, 'frontend/src/test/setup.js')],
    include: ['frontend/src/**/*.test.{js,jsx}'],
  },
  resolve: {
    // Vite-plugin-solid's default is to apply the `browser` condition for
    // Vitest runs; we add `solid` so direct imports of `solid-js/web`
    // resolve to the client build under jsdom.
    conditions: ['solid', 'browser'],
  },
});
