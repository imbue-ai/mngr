When a workspace server becomes unresponsive (HEALTHY -> STUCK transition),
the chrome titlebar now auto-navigates the content view to the recovery page
for the affected agent instead of attempting to render an in-titlebar banner.
The recovery page already redirects back to the original workspace URL once
the server is healthy again. The in-titlebar banner approach didn't work in
Electron because the banner was positioned outside the chrome WebContentsView's
bounds, where the content view covered it.
