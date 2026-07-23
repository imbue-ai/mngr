// The SuperTokens auth surfaces: the two-tab sign-up/sign-in AuthForm (shared
// by the standalone /auth page and the centered sign-in modal), the
// verify-your-email poller, the forgot-password form, the OAuth-popup close
// page, and the account-settings page. Ported wholesale from static/auth.js +
// templates/auth/*.jinja.
import m from "mithril";

import type {
  AccountSettingsBootIsland,
  AuthFormBootExtras,
  AuthFormBootIsland,
  CheckEmailBootIsland,
  OauthCloseBootIsland,
} from "../chrome_state";
import { getHost } from "../host";
import { MindsUIError, mountWithTeardown, readBootState, requireElement } from "../mount";
import { buttonClasses, TEXT_INPUT_CLASSES } from "../ui";
import { DialogCloseButton } from "./DialogCloseButton";

// Brand OAuth glyphs (multi-color, 24-viewBox) don't fit the monochrome Icon
// component, so they're inlined as static trusted SVG literals via m.trust --
// never interpolated data, mirroring the old auth.OauthIcon component.
const OAUTH_ICON_SVG: Record<"google" | "github", string> = {
  google:
    '<svg viewBox="0 0 24 24" class="w-[18px] h-[18px]">' +
    '<path d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92a5.06 5.06 0 0 1-2.2 3.32v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.1z" fill="#4285F4"/>' +
    '<path d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z" fill="#34A853"/>' +
    '<path d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z" fill="#FBBC05"/>' +
    '<path d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z" fill="#EA4335"/>' +
    "</svg>",
  github:
    '<svg viewBox="0 0 24 24" fill="#333" class="w-[18px] h-[18px]">' +
    '<path d="M12 0C5.37 0 0 5.37 0 12c0 5.31 3.435 9.795 8.205 11.385.6.105.825-.255.825-.57 0-.285-.015-1.23-.015-2.235-3.015.555-3.795-.735-4.035-1.41-.135-.345-.72-1.41-1.23-1.695-.42-.225-1.02-.78-.015-.795.945-.015 1.62.87 1.845 1.23 1.08 1.815 2.805 1.305 3.495.99.105-.78.42-1.305.765-1.605-2.67-.3-5.46-1.335-5.46-5.925 0-1.305.465-2.385 1.23-3.225-.12-.3-.54-1.53.12-3.18 0 0 1.005-.315 3.3 1.23.96-.27 1.98-.405 3-.405s2.04.135 3 .405c2.295-1.56 3.3-1.23 3.3-1.23.66 1.65.24 2.88.12 3.18.765.84 1.23 1.905 1.23 3.225 0 4.605-2.805 5.625-5.475 5.925.435.375.81 1.095.81 2.22 0 1.605-.015 2.895-.015 3.3 0 .315.225.69.825.57A12.02 12.02 0 0 0 24 12c0-6.63-5.37-12-12-12z"/>' +
    "</svg>",
};

// The auth cards use the larger radius (radius="lg" in the old TextInput).
const AUTH_INPUT_CLASSES = TEXT_INPUT_CLASSES.replace("rounded-md", "rounded-lg");
const OAUTH_POLL_INTERVAL_MS = 2000;
const OAUTH_TIMEOUT_MS = 3 * 60 * 1000;

// -- The two-tab AuthForm ----------------------------------------------------

type AuthTab = "signup" | "signin";

interface FieldError {
  signup: string | null;
  signin: string | null;
}

class AuthFormController {
  readonly extras: AuthFormBootExtras;
  tab: AuthTab;
  signupEmail = "";
  signupPassword = "";
  signinEmail = "";
  signinPassword = "";
  isSubmitting = false;
  // Per-tab error text (the OAuth "waiting" message reuses these slots, shown
  // in the accent style via `isWaitingMessage`).
  errors: FieldError = { signup: null, signin: null };
  isWaitingMessage = false;
  isOauthInFlight = false;

  private oauthPollTimer: ReturnType<typeof setInterval> | null = null;
  private oauthDeadline = 0;

  constructor(extras: AuthFormBootExtras) {
    this.extras = extras;
    this.tab = extras.default_to_signup ? "signup" : "signin";
  }

  stop(): void {
    if (this.oauthPollTimer !== null) {
      clearInterval(this.oauthPollTimer);
      this.oauthPollTimer = null;
    }
  }

  // Where a successful sign-in / verification round-trip lands. The modal
  // routes through the host adapter (Electron navigates the content view
  // behind the overlay AND dismisses it; browser mode rides the swap engine);
  // the standalone page navigates itself.
  private authNavigate(url: string): void {
    if (this.extras.is_modal) getHost().navigate(url);
    else window.location.href = url;
  }

  private postLoginUrl(): string {
    const returnTo = new URLSearchParams(window.location.search).get("return_to");
    return returnTo !== null && returnTo !== "" ? `/post-login?return_to=${encodeURIComponent(returnTo)}` : "/post-login";
  }

  // The modal was told where to land (return_to); the standalone page has no
  // such hint and goes through /post-login (which may carry its own
  // ?return_to=).
  private onAuthSuccess(): void {
    this.authNavigate(this.extras.return_to !== "" ? this.extras.return_to : this.postLoginUrl());
  }

  private verificationReturnTo(): string | null {
    const q = new URLSearchParams(window.location.search).get("return_to");
    if (q !== null && q !== "") return q;
    return this.extras.return_to !== "" ? this.extras.return_to : null;
  }

  private goToCheckEmail(): void {
    const returnTo = this.verificationReturnTo();
    this.authNavigate(`/auth/check-email${returnTo !== null ? `?return_to=${encodeURIComponent(returnTo)}` : ""}`);
  }

  showTab(tab: AuthTab): void {
    this.tab = tab;
    m.redraw();
  }

  private setError(tab: AuthTab, message: string): void {
    this.errors[tab] = message;
    this.isWaitingMessage = false;
    m.redraw();
  }

  async submitSignup(): Promise<void> {
    this.isSubmitting = true;
    this.errors.signup = null;
    m.redraw();
    try {
      const response = await fetch("/auth/api/signup", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: this.signupEmail, password: this.signupPassword }),
      });
      const data = (await response.json()) as { status?: string; message?: string };
      if (data.status === "OK") {
        this.goToCheckEmail();
        return;
      }
      this.setError("signup", data.message !== undefined && data.message !== "" ? data.message : "Sign-up failed");
    } catch (error) {
      this.setError("signup", `Network error: ${error instanceof Error ? error.message : String(error)}`);
    }
    this.isSubmitting = false;
    m.redraw();
  }

  async submitSignin(): Promise<void> {
    this.isSubmitting = true;
    this.errors.signin = null;
    m.redraw();
    try {
      const response = await fetch("/auth/api/signin", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: this.signinEmail, password: this.signinPassword }),
      });
      const data = (await response.json()) as { status?: string; message?: string; needsEmailVerification?: boolean };
      if (data.status === "OK") {
        if (data.needsEmailVerification === true) this.goToCheckEmail();
        else this.onAuthSuccess();
        return;
      }
      this.setError("signin", data.message !== undefined && data.message !== "" ? data.message : "Sign-in failed");
    } catch (error) {
      this.setError("signin", `Network error: ${error instanceof Error ? error.message : String(error)}`);
    }
    this.isSubmitting = false;
    m.redraw();
  }

  private showOauthWaiting(provider: string): void {
    const label = provider === "google" ? "Google" : provider === "github" ? "GitHub" : provider;
    const message = `Waiting for you to finish signing in with ${label} in the browser...`;
    this.errors.signup = message;
    this.errors.signin = message;
    this.isWaitingMessage = true;
    this.isOauthInFlight = true;
    m.redraw();
  }

  private stopOauth(): void {
    if (this.oauthPollTimer !== null) {
      clearInterval(this.oauthPollTimer);
      this.oauthPollTimer = null;
    }
    this.isOauthInFlight = false;
    m.redraw();
  }

  async startOauth(provider: string): Promise<void> {
    let flowId: string | null = null;
    try {
      const response = await fetch(`/auth/oauth/${provider}`);
      const data = (await response.json()) as { status?: string; error?: string; message?: string; flow_id?: string };
      if (data.status !== "OK") {
        window.alert(`Failed to start OAuth: ${data.error ?? data.message ?? "unknown error"}`);
        return;
      }
      flowId = data.flow_id ?? null;
      if (flowId === null) {
        window.alert("Failed to start OAuth: server did not return a flow_id");
        return;
      }
    } catch (error) {
      window.alert(`Failed to start OAuth: ${error instanceof Error ? error.message : String(error)}`);
      return;
    }
    this.showOauthWaiting(provider);
    this.oauthDeadline = performance.now() + OAUTH_TIMEOUT_MS;
    if (this.oauthPollTimer !== null) clearInterval(this.oauthPollTimer);
    this.oauthPollTimer = setInterval(() => void this.pollOauth(flowId), OAUTH_POLL_INTERVAL_MS);
  }

  private async pollOauth(flowId: string): Promise<void> {
    if (performance.now() > this.oauthDeadline) {
      this.stopOauth();
      window.alert("Sign-in timed out. Try again.");
      return;
    }
    try {
      const response = await fetch(`/auth/oauth/status/${flowId}`);
      const state = (await response.json()) as { status?: string; state?: string; error?: string };
      if (state.status !== "OK") {
        // Server forgot the flow (e.g. desktop server restart). Stop polling.
        this.stopOauth();
        window.alert("Sign-in lost track of this flow. Try again.");
        return;
      }
      if (state.state === "done") {
        if (this.oauthPollTimer !== null) clearInterval(this.oauthPollTimer);
        this.oauthPollTimer = null;
        this.onAuthSuccess();
        return;
      }
      if (state.state === "error") {
        this.stopOauth();
        window.alert(`Sign-in failed: ${state.error ?? "unknown error"}`);
        return;
      }
      // state === "running" -- keep polling.
    } catch {
      // Transient network blip; keep polling.
    }
  }
}

function errorNotice(controller: AuthFormController, tab: AuthTab, id: string): m.Children {
  const message = controller.errors[tab];
  if (message === null) return null;
  const classes = controller.isWaitingMessage
    ? "text-accent type-body mb-3 px-3 py-2 bg-accent/12 rounded-md border border-accent/30"
    : "px-3 py-2 rounded-md type-body my-2 bg-[var(--c-important-surface)] text-important";
  return m("div", { id, class: classes }, message);
}

function oauthButton(controller: AuthFormController, provider: "google" | "github", extra = ""): m.Children {
  const label = provider === "google" ? "Continue with Google" : "Continue with GitHub";
  return m(
    "button",
    {
      type: "button",
      "data-oauth": provider,
      disabled: controller.isOauthInFlight,
      class:
        "oauth-btn w-full py-2 px-3 bg-surface-primary border border-default rounded-lg type-body cursor-pointer " +
        `flex items-center justify-center gap-2 text-primary hover:bg-fill-hover disabled:opacity-40 ${extra}`,
      onclick: () => void controller.startOauth(provider),
    },
    [m.trust(OAUTH_ICON_SVG[provider]), ` ${label}`],
  );
}

function orDivider(): m.Children {
  return m("div", { class: "flex items-center my-4 text-tertiary type-helper" }, [
    m("span", { class: "flex-1 border-b border-default" }),
    m("span", { class: "px-3" }, "or"),
    m("span", { class: "flex-1 border-b border-default" }),
  ]);
}

function authInput(id: string, type: string, value: string, oninput: (v: string) => void, attrs: Record<string, unknown>): m.Children {
  return m("input", {
    id,
    type,
    class: AUTH_INPUT_CLASSES,
    required: true,
    value,
    oninput: (event: Event) => oninput((event.target as HTMLInputElement).value),
    ...attrs,
  });
}

function AuthForm(): m.Component<{ controller: AuthFormController }> {
  return {
    view(vnode) {
      const { controller } = vnode.attrs;
      const { extras } = controller;
      return [
        controller.tab === "signup"
          ? m("div", { id: "signup-tab" }, [
              m("h1", { class: "type-heading-lg text-primary mb-2" }, "Create account"),
              m(
                "p",
                { class: extras.intro !== "" ? "type-body text-primary mb-6" : "text-secondary mb-6" },
                extras.intro !== "" ? extras.intro : "Sign up to enable sharing",
              ),
              errorNotice(controller, "signup", "signup-error"),
              m(
                "form",
                {
                  id: "signup-form",
                  onsubmit: (event: Event) => {
                    event.preventDefault();
                    void controller.submitSignup();
                  },
                },
                [
                  m("div", { class: "mb-4" }, [
                    m("label", { for: "signup-email", class: "type-label text-primary block mb-1.5" }, "Email"),
                    authInput("signup-email", "email", controller.signupEmail, (v) => (controller.signupEmail = v), {
                      autocomplete: "email",
                    }),
                  ]),
                  m("div", { class: "mb-4" }, [
                    m("label", { for: "signup-password", class: "type-label text-primary block mb-1.5" }, "Password"),
                    authInput(
                      "signup-password",
                      "password",
                      controller.signupPassword,
                      (v) => (controller.signupPassword = v),
                      { autocomplete: "new-password", minlength: 8 },
                    ),
                  ]),
                  m(
                    "button",
                    {
                      type: "submit",
                      id: "signup-btn",
                      class: `${buttonClasses("primary", "lg")} w-full`,
                      disabled: controller.isSubmitting,
                    },
                    controller.isSubmitting ? "Creating account..." : "Create account",
                  ),
                ],
              ),
              orDivider(),
              oauthButton(controller, "google", "mb-2"),
              oauthButton(controller, "github"),
              m("div", { class: "text-center mt-4 type-body text-secondary" }, [
                "Already have an account? ",
                m(
                  "a",
                  {
                    href: "#",
                    class: "text-accent hover:underline font-semibold",
                    onclick: (event: Event) => {
                      event.preventDefault();
                      controller.showTab("signin");
                    },
                  },
                  "Sign in",
                ),
              ]),
            ])
          : m("div", { id: "signin-tab" }, [
              m("h1", { class: "type-heading-lg text-primary mb-2" }, "Sign in"),
              m(
                "p",
                { class: extras.intro !== "" ? "type-body text-primary mb-6" : "text-secondary mb-6" },
                extras.intro !== "" ? extras.intro : "Sign in to your Imbue account",
              ),
              errorNotice(controller, "signin", "signin-error"),
              m(
                "form",
                {
                  id: "signin-form",
                  onsubmit: (event: Event) => {
                    event.preventDefault();
                    void controller.submitSignin();
                  },
                },
                [
                  m("div", { class: "mb-4" }, [
                    m("label", { for: "signin-email", class: "type-label text-primary block mb-1.5" }, "Email"),
                    authInput("signin-email", "email", controller.signinEmail, (v) => (controller.signinEmail = v), {
                      autocomplete: "email",
                    }),
                  ]),
                  m("div", { class: "mb-4" }, [
                    m("label", { for: "signin-password", class: "type-label text-primary block mb-1.5" }, "Password"),
                    authInput(
                      "signin-password",
                      "password",
                      controller.signinPassword,
                      (v) => (controller.signinPassword = v),
                      { autocomplete: "current-password" },
                    ),
                  ]),
                  m(
                    "div",
                    { class: "text-right -mt-2 mb-4" },
                    m(
                      "a",
                      {
                        href: "/auth/forgot-password",
                        class: "text-accent hover:underline type-helper",
                        // In the modal, the forgot-password link must land in
                        // the content view and dismiss the overlay, not
                        // navigate the overlay iframe.
                        onclick: extras.is_modal
                          ? (event: Event) => {
                              event.preventDefault();
                              getHost().navigate("/auth/forgot-password");
                            }
                          : undefined,
                      },
                      "Forgot password?",
                    ),
                  ),
                  m(
                    "button",
                    {
                      type: "submit",
                      id: "signin-btn",
                      class: `${buttonClasses("primary", "lg")} w-full`,
                      disabled: controller.isSubmitting,
                    },
                    controller.isSubmitting ? "Signing in..." : "Sign in",
                  ),
                ],
              ),
              orDivider(),
              oauthButton(controller, "google", "mb-2"),
              oauthButton(controller, "github"),
              m("div", { class: "text-center mt-4 type-body text-secondary" }, [
                "Don't have an account? ",
                m(
                  "a",
                  {
                    href: "#",
                    class: "text-accent hover:underline font-semibold",
                    onclick: (event: Event) => {
                      event.preventDefault();
                      controller.showTab("signup");
                    },
                  },
                  "Create one",
                ),
              ]),
            ]),
      ];
    },
  };
}

function readAuthIsland(): AuthFormBootExtras {
  const island = readBootState() as AuthFormBootIsland;
  if (island.auth === undefined) {
    throw new MindsUIError("auth boot island is missing the auth slice");
  }
  return island.auth;
}

// Standalone /auth page: optional back link + info banner + the AuthForm.
export function mountAuthPage(target: Element | null): void {
  const el = requireElement(target, "auth page container");
  const extras = readAuthIsland();
  const controller = new AuthFormController(extras);
  mountWithTeardown(el, {
    view: () => [
      extras.return_to !== ""
        ? m(
            "div",
            { class: "mb-4" },
            m("a", { id: "auth-back-link", href: extras.return_to, class: "text-accent hover:underline font-semibold" }, "← Back to workspace setup"),
          )
        : null,
      extras.message !== ""
        ? m("div", { class: "px-3 py-2 rounded-md type-body my-2 bg-[var(--c-info-surface)] text-info" }, extras.message)
        : null,
      m(AuthForm, { controller }),
    ],
    onremove: () => controller.stop(),
  });
}

// Centered sign-in modal: dim backdrop + card + host-adapter dismissal.
export function mountSigninModal(target: Element | null): void {
  const el = requireElement(target, "signin modal container");
  const extras = readAuthIsland();
  const controller = new AuthFormController(extras);
  const dismiss = (): void => {
    if (window.minds !== undefined) getHost().closeModal();
    else window.location.href = extras.return_to !== "" ? extras.return_to : "/";
  };
  mountWithTeardown(el, {
    view: () =>
      m(
        "div",
        {
          id: "signin-modal-backdrop",
          class: "fixed inset-0 flex items-center justify-center bg-surface-overlay p-4",
          onclick: (event: Event) => {
            if (event.target === event.currentTarget) dismiss();
          },
        },
        m("div", { class: "relative bg-surface-primary rounded-lg shadow-overlay max-w-md w-full p-8 text-left" }, [
          m(DialogCloseButton, { onclick: dismiss }),
          m(AuthForm, { controller }),
        ]),
      ),
    onremove: () => controller.stop(),
  });
}

// -- Verify-your-email page --------------------------------------------------

export function mountCheckEmail(target: Element | null): void {
  const el = requireElement(target, "check-email container");
  const island = readBootState() as CheckEmailBootIsland;
  if (island.check_email === undefined) {
    throw new MindsUIError("check-email boot island is missing the check_email slice");
  }
  const email = island.check_email.email;
  let isVerified = false;
  let resendLabel = "Resend verification email";
  let isResendDisabled = false;

  const pollTimer = setInterval(() => void poll(), 3000);
  async function poll(): Promise<void> {
    try {
      const response = await fetch("/auth/api/email-verified");
      const data = (await response.json()) as { verified?: boolean };
      if (data.verified === true) {
        clearInterval(pollTimer);
        isVerified = true;
        m.redraw();
        const returnTo = new URLSearchParams(window.location.search).get("return_to");
        const dest = returnTo !== null && returnTo !== "" ? `/post-login?return_to=${encodeURIComponent(returnTo)}` : "/post-login";
        setTimeout(() => {
          window.location.href = dest;
        }, 1500);
      }
    } catch {
      // Transient; the next tick retries.
    }
  }

  async function resend(): Promise<void> {
    isResendDisabled = true;
    resendLabel = "Sending...";
    m.redraw();
    try {
      await fetch("/auth/api/resend-verification", { method: "POST" });
      resendLabel = "Sent. Check your inbox";
      m.redraw();
      setTimeout(() => {
        isResendDisabled = false;
        resendLabel = "Resend verification email";
        m.redraw();
      }, 5000);
    } catch {
      isResendDisabled = false;
      resendLabel = "Resend verification email";
      m.redraw();
    }
  }

  mountWithTeardown(el, {
    view: () =>
      m("div", { class: "text-center" }, [
        m("h1", { class: "type-heading-lg text-primary mb-2" }, "Check your email"),
        m("p", { class: "text-secondary mb-3" }, ["We sent a verification link to ", m("strong", email)]),
        m("p", { class: "type-body text-secondary mb-4" }, "Click the link in the email to verify your account, then come back here."),
        isVerified
          ? m("div", { id: "success-msg", class: "px-3 py-2 rounded-md type-body my-2 bg-[var(--c-success-surface)] text-success" }, "Email verified. Redirecting...")
          : m("div", { id: "status-msg", class: "px-3 py-2 rounded-md type-body my-2 bg-[var(--c-info-surface)] text-info" }, "Waiting for verification..."),
        m(
          "button",
          {
            type: "button",
            id: "resend-btn",
            class: `${buttonClasses("primary", "lg")} w-full mt-4`,
            disabled: isResendDisabled,
            onclick: () => void resend(),
          },
          resendLabel,
        ),
        m("div", { class: "text-center mt-3 type-body text-secondary" }, m("a", { href: "/", class: "text-accent hover:underline" }, "Go to home")),
      ]),
    onremove: () => clearInterval(pollTimer),
  });
}

// -- Forgot-password page ----------------------------------------------------

export function mountForgotPassword(target: Element | null): void {
  const el = requireElement(target, "forgot-password container");
  let email = "";
  let successMessage: string | null = null;
  let errorMessage: string | null = null;
  let isSubmitting = false;

  async function submit(): Promise<void> {
    isSubmitting = true;
    errorMessage = null;
    m.redraw();
    try {
      const response = await fetch("/auth/api/forgot-password", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email }),
      });
      const data = (await response.json()) as { message?: string };
      successMessage = data.message ?? "";
    } catch {
      errorMessage = "Network error";
    }
    isSubmitting = false;
    m.redraw();
  }

  mountWithTeardown(el, {
    view: () => [
      m("h1", { class: "type-heading-lg text-primary mb-2" }, "Reset password"),
      m("p", { class: "text-secondary mb-6" }, "Enter your email to receive a reset link"),
      errorMessage !== null
        ? m("div", { id: "error-msg", class: "px-3 py-2 rounded-md type-body my-2 bg-[var(--c-important-surface)] text-important" }, errorMessage)
        : null,
      successMessage !== null
        ? m("div", { id: "success-msg", class: "px-3 py-2 rounded-md type-body my-2 bg-[var(--c-success-surface)] text-success" }, successMessage)
        : null,
      m(
        "form",
        {
          id: "forgot-form",
          onsubmit: (event: Event) => {
            event.preventDefault();
            void submit();
          },
        },
        [
          m("div", { class: "mb-4" }, [
            m("label", { for: "email", class: "type-label text-primary block mb-1.5" }, "Email"),
            m("input", {
              id: "email",
              type: "email",
              class: AUTH_INPUT_CLASSES,
              required: true,
              autocomplete: "email",
              value: email,
              oninput: (event: Event) => (email = (event.target as HTMLInputElement).value),
            }),
          ]),
          m(
            "button",
            { type: "submit", id: "submit-btn", class: `${buttonClasses("primary", "lg")} w-full`, disabled: isSubmitting },
            "Send reset link",
          ),
        ],
      ),
      m(
        "div",
        { class: "text-center mt-4 type-body text-secondary" },
        m("a", { href: "/auth/login", class: "text-accent hover:underline font-semibold" }, "Back to sign in"),
      ),
    ],
  });
}

// -- OAuth-popup close page --------------------------------------------------

export function mountOauthClose(target: Element | null): void {
  const el = requireElement(target, "oauth-close container");
  const island = readBootState() as OauthCloseBootIsland;
  if (island.oauth_close === undefined) {
    throw new MindsUIError("oauth-close boot island is missing the oauth_close slice");
  }
  const { email, display_name } = island.oauth_close;
  mountWithTeardown(el, {
    view: () =>
      m("div", { class: "text-center" }, [
        m("h1", { class: "type-heading-lg text-primary mb-2" }, "Signed in"),
        m("p", { class: "text-secondary mb-3" }, ["Signed in as ", m("strong", display_name !== "" ? display_name : email)]),
        m("p", { class: "type-body text-secondary" }, "You can close this tab and return to the app."),
      ]),
  });
}

// -- Account settings page ---------------------------------------------------

export function mountAccountSettings(target: Element | null): void {
  const el = requireElement(target, "account-settings container");
  const island = readBootState() as AccountSettingsBootIsland;
  if (island.account_settings === undefined) {
    throw new MindsUIError("account-settings boot island is missing the account_settings slice");
  }
  const { email, display_name, provider, user_id_prefix } = island.account_settings;
  let isSigningOut = false;

  async function signOut(): Promise<void> {
    isSigningOut = true;
    m.redraw();
    try {
      await fetch("/auth/api/signout", { method: "POST" });
    } finally {
      window.location.href = "/";
    }
  }

  function row(label: string, value: m.Children): m.Children {
    return m("div", { class: "flex justify-between items-center py-3 border-b border-subtle" }, [
      m("span", { class: "type-body text-secondary" }, label),
      value,
    ]);
  }

  mountWithTeardown(el, {
    view: () => [
      m("h1", { class: "type-heading-lg text-primary mb-2" }, "Account settings"),
      m("p", { class: "text-secondary mb-6" }, "Manage your Imbue account"),
      row("Email", m("span", { class: "text-primary font-semibold" }, email)),
      display_name !== "" ? row("Name", m("span", { class: "text-primary font-semibold" }, display_name)) : null,
      row("Auth provider", m("span", { class: "text-primary font-semibold" }, provider)),
      row("User ID prefix", m("span", { class: "text-primary font-mono type-label" }, user_id_prefix)),
      m("div", { class: "mt-6 flex flex-col gap-2" }, [
        provider === "email"
          ? m("a", { href: "/auth/forgot-password", class: `${buttonClasses("secondary", "lg")} w-full` }, "Change password")
          : null,
        m(
          "button",
          { type: "button", id: "signout-btn", class: `${buttonClasses("danger", "lg")} w-full`, disabled: isSigningOut, onclick: () => void signOut() },
          "Sign out",
        ),
      ]),
      m(
        "div",
        { class: "text-center mt-4 type-body text-secondary" },
        m("a", { href: "/", class: "text-accent hover:underline font-semibold" }, "Back to home"),
      ),
    ],
  });
}
