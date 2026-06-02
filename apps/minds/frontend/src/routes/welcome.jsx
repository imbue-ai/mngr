import { createSignal } from 'solid-js';
import { ButtonLink } from '../components/ui/ButtonLink.jsx';
import { Button } from '../components/ui/Button.jsx';

// Welcome / splash page. First-time-user entry point: sign up, log in,
// or proceed without an Imbue account (with a confirmation dialog so the
// user knows what they lose).
export function WelcomeRoute() {
  const [skipDialogOpen, setSkipDialogOpen] = createSignal(false);

  return (
    <div class="min-h-screen flex items-center justify-center">
      <div class="max-w-sm w-full px-6 text-center">
        <h1 class="text-3xl font-bold text-zinc-900 mb-2">Welcome to Minds</h1>
        <p class="text-zinc-500 text-sm mb-8">Run persistent, autonomous AI agents</p>

        <div class="flex flex-col gap-3 mb-6">
          <ButtonLink href="/auth/signup" variant="primary" block>
            Sign Up
          </ButtonLink>
          <ButtonLink href="/auth/login" variant="secondary" block>
            Log In
          </ButtonLink>
        </div>

        <button
          type="button"
          id="skip-account-btn"
          class="text-xs text-zinc-400 hover:text-zinc-600 transition-colors cursor-pointer"
          onClick={() => setSkipDialogOpen(true)}
        >
          Continue without an account
        </button>

        <div
          id="skip-dialog"
          class={`${skipDialogOpen() ? '' : 'hidden '}fixed inset-0 z-50 flex items-center justify-center bg-black/30`}
          onClick={(event) => {
            if (event.target === event.currentTarget) setSkipDialogOpen(false);
          }}
        >
          <div class="bg-white rounded-xl shadow-lg border border-zinc-200 max-w-sm w-full mx-4 p-6 text-left">
            <h2 class="text-lg font-semibold text-zinc-900 mb-3">
              Continue without an account?
            </h2>
            <p class="text-sm text-zinc-600 mb-2">An Imbue account enables:</p>
            <ul class="text-sm text-zinc-600 list-disc pl-5 mb-4 space-y-1">
              <li>Project sharing</li>
              <li>Imbue-hosted cloud projects</li>
              <li>No need to bring your own API keys</li>
            </ul>
            <p class="text-xs text-zinc-400 mb-5">
              You can always create an account later by clicking "log in" in the upper right.
            </p>
            <div class="flex justify-end gap-3">
              <Button
                variant="secondary"
                id="skip-cancel-btn"
                onClick={() => setSkipDialogOpen(false)}
              >
                Cancel
              </Button>
              <ButtonLink href="/create" variant="primary" id="skip-continue-btn">
                Continue
              </ButtonLink>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
