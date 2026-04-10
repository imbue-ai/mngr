import { defineConfig } from "vite";
import tailwindcss from "@tailwindcss/vite";
import path from "path";

export default defineConfig({
  plugins: [tailwindcss()],
  publicDir: "media",
  root: ".",
  build: {
    outDir: path.resolve(__dirname, "../imbue/claude_web_chat/static"),
    emptyOutDir: true,
  },
  server: {
    proxy: {
      "/api": "http://localhost:8000",
    },
  },
});
