import { execSync } from "child_process";
import { describe, expect, it } from "vitest";

const FRONTEND_ROOT = new URL("..", import.meta.url).pathname;

function run(command: string): { exitCode: number; output: string } {
  try {
    const output = execSync(command, { cwd: FRONTEND_ROOT, encoding: "utf-8", stdio: "pipe" });
    return { exitCode: 0, output };
  } catch (error) {
    const execError = error as { status: number; stdout: string; stderr: string };
    return { exitCode: execError.status, output: `${execError.stdout}\n${execError.stderr}` };
  }
}

describe("code quality", () => {
  it("eslint produces no issues", () => {
    const result = run("npx eslint src/");
    expect(result.exitCode, `eslint found issues:\n${result.output}`).toBe(0);
  });

  it("prettier formatting has been applied", () => {
    const result = run("npx prettier --check 'src/**/*.{ts,css,html}' '*.{ts,html}'");
    expect(result.exitCode, `prettier found unformatted files:\n${result.output}`).toBe(0);
  });
});
