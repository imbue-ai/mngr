import { defineConfig } from "vitest/config";

export default defineConfig({
  // Root the run at this directory (not the pnpm CWD, apps/minds) so the
  // include pattern below resolves against frontend/.
  root: import.meta.dirname,
  test: {
    // Components mount into a real DOM tree in tests (m.mount + events), so
    // every test file gets a jsdom document rather than a bare node context.
    environment: "jsdom",
    include: ["src/**/*.test.ts"],
  },
});
