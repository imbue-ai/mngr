- The start flow's account step can no longer strand a user on an account they abandoned. Creating or signing in to a second account after taking back the first (for example, a first sign-up whose email was never verified) now settles on the new account: an account that was already signed in no longer answers the press, and the cloud create and its email-verification check run under the account the flow settled on rather than the install's default account.

- With an account already signed in, choosing Imbue Cloud now asks the account question, naming the signed-in account's email in bold and offering "Continue" and "Use a different account" (which opens the browser sign-in, where the user can sign in or create an account), instead of silently using that account, so the answer has a "Change answer" undo like every other one.

- A cloud create from the start flow whose request fails to build no longer leaves every "Change answer" button hidden; it is reported as a failed attempt and the where-to-run question is asked again.

- Answering the account question after starting and closing a sign-in (for example, pressing "Use a different account", closing the dialog, then "Continue") now replaces that sign-in, so a browser sign-up that completes later no longer re-answers the question and sends a second cloud create.

- Pressing "Continue" on the account step after the offered account was signed out elsewhere now asks the account question again about the account signed in now, instead of creating the workspace under an account that is no longer signed in.

- A cloud create whose account was signed out after the account step settled on it (for example while the flow waited on that account's email verification) is no longer sent; the flow says the account is no longer signed in and asks again where to run the workspace.

- An email-verification check still in flight when the account step switches to another account no longer decides the new account's create; the new account's email is checked on its own.

- The start flow leaves more room under its latest turn when it scrolls a new one into view, so the last question's buttons no longer sit tight against the bottom of the window.

- The start flow's "Change answer" undo now sits just after an answer's bubble, on the page, instead of inside the grey bubble.

- The start flow's first answer, to "Is it ok if we report errors to help improve Studio?", can now be taken back like the others: its undo asks the question again, and the new answer is saved as the error-reporting setting.
