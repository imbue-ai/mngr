import { onlineManager } from "@tanstack/query-core";
import { afterEach, describe, expect, it } from "vitest";
import { createAppQueryClient, getAppQueryClient } from "./queryClient";

afterEach(() => {
  onlineManager.setOnline(true);
});

describe("the app query client", () => {
  it("still reads from the desktop client while the network is reported down", async () => {
    onlineManager.setOnline(false);
    const client = createAppQueryClient();

    const answer = await client.fetchQuery({
      queryKey: ["probe"],
      queryFn: () => Promise.resolve("answered over loopback"),
    });

    expect(answer).toBe("answered over loopback");
  });

  it("reports a failed read once rather than retrying it", async () => {
    const client = createAppQueryClient();
    let attemptCount = 0;

    await expect(
      client.fetchQuery({
        queryKey: ["probe"],
        queryFn: () => {
          attemptCount += 1;
          return Promise.reject(new Error("the connector is down"));
        },
      }),
    ).rejects.toThrow("the connector is down");

    expect(attemptCount).toBe(1);
  });

  it("hands every caller the same client", () => {
    expect(getAppQueryClient()).toBe(getAppQueryClient());
  });
});
