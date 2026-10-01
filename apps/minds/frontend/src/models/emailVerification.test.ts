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
  it("posts to the same resource and reads an accepted send as sent", async () => {
    const methods: Array<string | undefined> = [];
    const fetcher = async (_url: string, init?: RequestInit): Promise<Response> => {
      methods.push(init?.method);
      return jsonResponse({ sent: true, email: "a@b.com" });
    };
    expect(await resendVerificationEmail("a@b.com", fetcher)).toBe("sent");
    expect(methods).toEqual(["POST"]);
  });

  it("separates a cooldown-suppressed send from one that never went out", async () => {
    // The server answered: a mail really did go out moments ago.
    expect(await resendVerificationEmail("a@b.com", async () => jsonResponse({ sent: false }))).toBe("suppressed");
    // Nothing reached the server, so nothing was sent and the view must not say otherwise.
    expect(await resendVerificationEmail("a@b.com", async () => jsonResponse({}, 409))).toBe("failed");
    expect(await resendVerificationEmail("a@b.com", async () => Promise.reject(new Error("offline")))).toBe("failed");
  });
});
