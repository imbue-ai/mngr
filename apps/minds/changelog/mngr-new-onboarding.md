The onboarding's second pass (`specs/new-onboarding/spec.md`).

- The start flow's opener reads "Imbue Studio is a new kind of personal computer. It is honest software."; the error-reporting "See more" opens with why we ask first; the where-to-run question drops its sentence about what a workspace is.

- The creation page replaces the reading material and the three numbered ways to start. For the first workspace (and any create whose form had "Use onboarding questions?" on), the agent asks while the workspace is built whether you know what to build ("I'm not sure", "No", "Yes"), and if not, whether to see examples of what others have made or describe a problem; the loading box stays hidden until the answer. A workspace ready by then is entered at once; one still being built gets "Great, let me just finish setting up your workspace..." with the box under it. Other creates show the box at once. The ready line stands a double gap over what precedes it, and the settings you chose are folded behind a "Details" chevron in your own turn.

- The create form's advanced view gains "Use onboarding questions?" (`is_onboarding_driven_by_agent` on `POST /api/v1/workspaces`, off by default; the start flow's creates turn it on), kept by a retry and by a reload of the creation page.

- The seeded welcome chat carries the questions and your answers, and closes on a welcome written for them: "Your workspace is ready." over an invitation to say what you want to build, or to describe what is annoying, overwhelming, or tedious (with one example complaint), for a reader who asked to see examples too, who saw one short recording of a to-do list app others have built on the creation page right after that answer (the chat's transcript carries it as a markdown image).

- The loading box's expandable log can be scrolled while a workspace is being created: it stays pinned to its end only while you are reading its end, instead of being reset to the end on every redraw.

- The desktop client seeds a workspace's welcome chat from the latest conversation the creation page handed over, not the first: a page left with an onboarding question open and answered on a later visit, before the workspace is ready, opens the workspace on the answer.

- The recordings are hosted on the new `onboarding-media` R2 bucket (`https://onboarding-media.imbueminds.com`) under content-addressed names, referenced from `apps/minds/frontend/src/models/onboardingMedia.ts`; nothing is committed. Provisioning the bucket and publishing to it is `docs/deploy/setup/onboarding-media.md`.

- Embed contract v9 adds `minds:highlight-chrome` (workspace to embedder): the workspace's tour asks the chrome to pulse a ring around the workspace switcher, a workspace tab, or the help button while a card talks about it, and to clear it with `null`. The titlebar draws the ring; the frame clears it when it navigates or unmounts.

- The onboarding's words follow the v2 copy document: the opener ("Studio is a new kind of personal computer, built on honest software."), the five manifesto details, the error-reporting "See more" and its buttons ("No thanks", "Yes, send reports"), the where-to-run question and the Imbue Cloud bullets ("Use it from your phone", "Easy to share with others", in the create form's card too), the returning-user link ("I have an account (log in)"), the account question (the reply to Use Imbue Cloud while signed out now opens "Great."), the sign-in window's intros, the setup line, the to-do list showcase's description, the ready line ("You're all set."), and the welcome's two lines. The loading box is headed by one bold "Setting up your workspace" beside its dot, without the workspace's name, and its status line steps through "Getting things ready", "Building your workspace", "Installing your tools", and "Almost there" on every launch mode. The fallback consent page shares the "See more" words.

