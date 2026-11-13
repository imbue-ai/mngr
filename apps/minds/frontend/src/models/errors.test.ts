import { describe, expect, it } from "vitest";
import {
  NO_RESPONSE_SENTENCE,
  bodyFromText,
  errorFromException,
  errorFromResponse,
  errorFromSentenceResponse,
  bugReportTextFor,
  sentenceUnderTitle,
} from "./errors";

describe("errorFromResponse", () => {
  it("names what failed and keeps the status and server text for the details", () => {
    const error = errorFromResponse("Couldn't rename this workspace", {
      status: 500,
      body: { error: "Internal Server Error: KeyError('host')" },
    });

    expect(error.message).toMatch(
      /^Couldn't rename this workspace\. Try again\./,
    );
    expect(error.message).not.toContain("KeyError");
    expect(error.details).toBe(
      "HTTP 500\nInternal Server Error: KeyError('host')",
    );
  });

  it("shows a reason the caller vouches for, still keeping the response behind it", () => {
    const error = errorFromResponse(
      "Couldn't turn alpha off",
      { status: 409, body: { error: "alpha has 2 workspaces on it" } },
      {
        plainReason:
          "You can't turn off alpha while 2 workspaces are still on it.",
      },
    );

    expect(error.message).toBe(
      "You can't turn off alpha while 2 workspaces are still on it.",
    );
    expect(error.details).toBe("HTTP 409\nalpha has 2 workspaces on it");
  });

  it("says Imbue Studio didn't respond, not that the request failed, when no response arrived", () => {
    const error = errorFromResponse("Couldn't rename this workspace", {
      status: 0,
      body: { error: "Could not reach the app server." },
    });

    expect(error.message).toBe(NO_RESPONSE_SENTENCE);
    expect(error.details).toBe("No response.");
  });

  it("keeps a verdict that rides under the server's sentence", () => {
    const error = errorFromResponse(
      "Couldn't start an agent in this workspace",
      {
        status: 502,
        body: { error: "Couldn't start an agent.", detail: "mngr: no account" },
      },
    );

    expect(error.details).toBe(
      "HTTP 502\nCouldn't start an agent.\nmngr: no account",
    );
  });

  it("keeps whatever else a body carried beside its sentence", () => {
    const error = errorFromResponse("Couldn't resize this machine", {
      status: 409,
      body: { error: "An operation is in progress.", operation: "restart" },
    });

    expect(error.details).toBe(
      'HTTP 409\nAn operation is in progress.\n{"operation":"restart"}',
    );
  });

  it("shows a sentence the body repeats under two keys once", () => {
    const error = errorFromResponse("Couldn't start this workspace", {
      status: 502,
      body: { error: "Host unreachable.", detail: "Host unreachable." },
    });

    expect(error.details).toBe("HTTP 502\nHost unreachable.");
  });

  it("drops a sentence key the body sent empty rather than printing it as JSON", () => {
    const error = errorFromResponse("Couldn't save the auto-update setting", {
      status: 500,
      body: { error: "No space left on device", detail: "" },
    });

    expect(error.details).toBe("HTTP 500\nNo space left on device");
  });

  it("keeps a sentence key that holds something other than a sentence", () => {
    const error = errorFromResponse("Couldn't save this change", {
      status: 500,
      body: { error: "Nope.", detail: { field: "name" } },
    });

    expect(error.details).toBe('HTTP 500\nNope.\n{"detail":{"field":"name"}}');
  });

  it("keeps a body with no sentence in it whole", () => {
    const error = errorFromResponse("Couldn't save this change", {
      status: 422,
      body: { field: "name", reason: "too_long" },
    });

    expect(error.details).toBe(
      'HTTP 422\n{"field":"name","reason":"too_long"}',
    );
  });

  it.each(["false", "null"])(
    "keeps a bare JSON value the body held (%s), as its text",
    (text) => {
      const error = errorFromResponse("Couldn't save this change", {
        status: 500,
        body: bodyFromText(text),
      });

      expect(error.details).toBe(`HTTP 500\n${text}`);
    },
  );
});

describe("a failure that can't be tried again", () => {
  it("names what failed without asking to try again, even when no response arrived", () => {
    for (const status of [500, 0]) {
      const error = errorFromResponse(
        "Imbue Studio couldn't show this request",
        { status, body: null },
        { isRetryable: false },
      );

      expect(error.message).toBe(
        "Imbue Studio couldn't show this request. If this persists, report a bug.",
      );
    }
  });
});

describe("errorFromException", () => {
  it("puts what was thrown behind the sentence", () => {
    const error = errorFromException(
      "Couldn't load your update settings",
      new Error("Error invoking remote method 'updates:get'"),
    );

    expect(error.message).toMatch(
      /^Couldn't load your update settings\. Try again\./,
    );
    expect(error.details).toBe("Error invoking remote method 'updates:get'");
  });
});

describe("errorFromSentenceResponse", () => {
  it("shows the route's own sentence and keeps its detail behind it", () => {
    const error = errorFromSentenceResponse("Couldn't connect Slack", {
      status: 409,
      body: {
        error: "Slack is already connected to another workspace.",
        detail: "grant 7f3 held by agent-cd34",
      },
    });

    expect(error.message).toBe(
      "Slack is already connected to another workspace.",
    );
    expect(error.details).toBe("HTTP 409\ngrant 7f3 held by agent-cd34");
  });

  it("says Imbue Studio didn't respond, not the stand-in error, when no response arrived", () => {
    const error = errorFromSentenceResponse("Couldn't connect Slack", {
      status: 0,
      body: { error: "Could not reach the app server." },
    });

    expect(error.message).toBe(NO_RESPONSE_SENTENCE);
    expect(error.details).toBe("No response.");
  });

  it("falls back to naming what failed when the refusal carries no sentence", () => {
    const error = errorFromSentenceResponse("Couldn't connect Slack", {
      status: 500,
      body: { detail: "KeyError('grant')" },
    });

    expect(error.message).toMatch(/^Couldn't connect Slack\. Try again\./);
    expect(error.details).toBe("HTTP 500\nKeyError('grant')");
  });
});

describe("sentenceUnderTitle", () => {
  it("drops a lead the dialog's title already says, and keeps a sentence that says something else", () => {
    expect(
      sentenceUnderTitle(
        'Couldn\'t start "alpha". Cannot connect to Docker daemon.',
        'Couldn\'t start "alpha"',
      ),
    ).toBe("Cannot connect to Docker daemon.");
    expect(
      sentenceUnderTitle(
        'Couldn\'t start "alpha". Try again. If this persists, report a bug.',
        'Couldn\'t start "alpha"',
      ),
    ).toBe("Try again. If this persists, report a bug.");
    expect(
      sentenceUnderTitle(
        "This workspace has been retired and cannot be started again.",
        'Can\'t start "alpha"',
      ),
    ).toBe("This workspace has been retired and cannot be started again.");
  });
});

describe("bugReportTextFor", () => {
  it("sends the sentence as text, without the markup it was written in", () => {
    expect(
      bugReportTextFor({
        message:
          "The SSO session has expired. To fix it, *run `aws sso login`*.",
        details: "ClientError: Token has expired",
      }),
    ).toBe(
      "The SSO session has expired. To fix it, run aws sso login.\n\n" +
        "ClientError: Token has expired",
    );
  });

  it("keeps a command's own asterisks, which are not the ask's", () => {
    expect(
      bugReportTextFor({
        message: "*Run `rm *.log`, then check again.*",
        details: "",
      }),
    ).toBe("Run rm *.log, then check again.");
  });
});
