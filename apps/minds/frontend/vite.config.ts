import { defineConfig } from "vite";
import tailwindcss from "@tailwindcss/vite";
import path from "path";

// Builds the SPA bundle straight into the desktop client's static tree so
// Flask serves real built assets (there is no dev server / HMR in the loop;
// `pnpm dev` runs this same build in --watch mode). `manifest: true` emits
// .vite/manifest.json, which the Flask index route reads to resolve the
// hashed entry filenames.
export default defineConfig({
  // Flask serves the bundle under /_static/ui/, so asset URLs the build writes (the headline font's files) are
  // rooted there.
  base: "/_static/ui/",
  plugins: [tailwindcss()],
  build: {
    outDir: path.resolve(__dirname, "../imbue/minds/desktop_client/static/ui"),
    emptyOutDir: true,
    manifest: true,
    rollupOptions: {
      input: path.resolve(__dirname, "src/index.ts"),
    },
  },
});
