In light mode the app's outer color is now the first run's cream instead of white: the window around every page, the title bar outside a workspace, and the home page's ground. Text, borders and fills on that cream are drawn in the first run's deep brown, with secondary and tertiary text a step stronger than on white, and stay black on white surfaces (cards, modals, menus, tooltips). Inside a workspace the title bar keeps the workspace's color, except while a first-run page is on screen, where it stays cream. Dark mode is unchanged.

- The first run's headlines are set in Besley Medium, now bundled with the app (SIL Open Font License), at a 1.15 line height. The sign-in headline sizes to the page (5.5% of its width, 56px to 88px) and breaks after "your"; the welcome is 40px, with "What type of studio do you want to make?" under it in the heading style.

- "Help improve Studio" sits centered at the sign-in page's foot and its label toggles the checkbox; what it means, with the privacy policy link, shows in a tooltip on hover or keyboard focus. "Use without an account" moves to the bottom-right corner with a tooltip saying what an account adds. Neither link is underlined.

- The browser sign-in dialog and the create form opened from the sign-in page fade the page into cream instead of dimming it.

- The first run's buttons (Sign in, Continue, Try again) share one shape and size, and "Send the email again" is a smaller outlined button. The first run's text uses the app's type styles.

- A new workspace's suggested color starts at the deep brown instead of the dark green, then runs through the palette, the color the fewest of your workspaces wear first; once every color is worn it goes around again (it used to stay on the first color). Workspaces with no saved color still show the dark green.

- The home page's Workspaces and Apps tabs have a little more space between them.

- While a new workspace's first-run provider page ("Your own cloud computer, any AI") covers it, the title bar stays cream; when the owner connects a provider or skips, the workspace's color grows from the window's center over the window and lifts off the workspace. This needs a workspace template that reports the page (embed contract v17, `minds:first-run-provider-step`); an older one keeps today's behavior.

- While the first workspace is being built, the welcome says "We're setting up your workspace" with the time left under the bar ("6 minutes left") when the wait is two minutes or more, and "Just a sec, we're setting up your workspace" otherwise. The bar is as wide as its line.

- Every progress bar says the time left more briefly: "6 minutes left", "1 minute left", "Less than a minute left" (was "About 6 minutes remaining" and so on).
