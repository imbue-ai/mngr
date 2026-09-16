import { describe, expect, it } from "vitest";
import { jsonResponse } from "../testing";
import { fetchIsEmailVerified, resendVerificationEmail } from "./emailVerification";

describe("fetchIsEmailVerified", () => {
  it("asks the app about the named email and reads the verdict", async () => {
    const urls: string[] = [];
    const fetcher = async (url: string): Promise<Response> => {
      urls.push(url);
      return jsonResponse({ verified: true, email: "alice@example.com" });
    };
    expect(await fetchIsEmailVerified("alice@example.com", fetcher)).toBe(true);
    expect(urls).toEqual(["/accounts/verification?email=alice%40example.com"]);
  });

  it("rejects when the app could not find out, rather than guessing", async () => {
    await expect(fetchIsEmailVerified("a@b.com", async () => jsonResponse({}, 502))).rejects.toThrow();
    await expect(fetchIsEmailVerified("a@b.com", async () => jsonResponse({ email: "a@b.com" }))).rejects.toThrow();
  });
});

describe("resendVerificationEmail", () => {
  it("posts to the same resource and reports whether an email went out", async () => {
    const methods: Array<string | undefined> = [];
    const fetcher = async (_url: string, init?: RequestInit): Promise<Response> => {
      methods.push(init?.method);
      return jsonResponse({ sent: false, email: "a@b.com" });
    };
    expect(await resendVerificationEmail("a@b.com", fetcher)).toBe(false);
    expect(methods).toEqual(["POST"]);
  });

  it("reads a failed request as nothing sent", async () => {
    expect(await resendVerificationEmail("a@b.com", async () => jsonResponse({}, 409))).toBe(false);
    expect(await resendVerificationEmail("a@b.com", async () => Promise.reject(new Error("offline")))).toBe(false);
  });
});
