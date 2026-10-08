# Start flow

Understanding this behavior corpus calls for the behaviors skill; consult it when reading this file.

The *start flow* is the first-run conversation at "/start" that an installation which has never been taken past it lands on (when it does is specified in `home-page/`).
It asks its questions one at a time as a chat, and ends when a workspace create goes out or the user leaves for the home page having signed in to an existing account.

## Terms

An *answer* is a user turn that replies to one of the flow's questions.
A *changeable* answer carries a "Change answer" control; pressing it takes that answer back, drops everything said after it, and asks that question again.
Every answer is changeable except the answers to the email-verification question, which record a fact about the world rather than a choice.
Changing the answer to the reporting question that closes the opening exchange asks that question again, and the new answer replaces the saved error-reporting setting.

The *account step* is the question that settles which Imbue account a cloud workspace runs under.
It is asked on every cloud answer: signed out, it offers to create an account or sign in; with an account already signed in, it names that account and offers to continue with it or to use a different one, which opens the same browser sign-in, where an account can be signed in to or created.
The account the account step *settled on* is the one its answer names: the account continued with, or the account a sign-in started from that question reports.

A *cloud create* is the workspace create the flow sends itself when the user answers that the workspace should run on Imbue Cloud; the custom answer instead opens the create form, whose submission is the create.

*Stranded* means no sequence of actions available to the user -- pressing what the flow offers, taking answers back, closing dialogs, signing in and verifying email in the browser -- leads to a workspace create.

## Out of scope

- Which screen the app shows at launch and when "/" hands over to the start flow -- specified in `home-page/` and by the Electron shell.
- The create form's own contents, and the creation page the flow hands over to once a create goes out.
- The hosted account pages the sign-in opens in the browser.
