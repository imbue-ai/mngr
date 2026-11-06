Studio now notifies you about two error states you would otherwise miss while its window is hidden or in the background. Each one arrives as a notification-feed entry (the bell, an in-app toast, and an OS banner when no focused window can show the toast), under the existing notification toggle and delivery style:

- An Imbue account signed out because its session expired. Clicking it opens the accounts page, where you can sign in again.

- A workspace that stopped responding and couldn't be started again. Clicking it opens the workspace and its recovery card.

A failed workspace is announced once per outage, and no more than once every 30 minutes, so a workspace that keeps failing doesn't send repeated notifications.

Clicking an app notification that names its own page, such as the notice that sharing couldn't be turned on because the shared-workspace limit was reached, now opens that page directly instead of the workspace's backups page.
