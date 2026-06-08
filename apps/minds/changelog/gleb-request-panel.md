The right-side requests panel is gone: pending permission requests now live
in an inbox modal opened from the same titlebar bell, with a master/detail
layout. Opening the inbox no longer resizes or shifts the workspace -- it
overlays the window the same way the permission dialog already did.

Approving or denying a request keeps the inbox open and auto-advances to
the next pending item. Browser-mode deep links are now ``/inbox?selected=<id>``
(the standalone ``/requests/<id>`` page has been removed).
