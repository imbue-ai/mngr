Added a `FAILED` outcome to the latchkey permission-grant flow. Previously, if
the browser sign-in (including the one-off `latchkey auth browser-prepare` step)
failed when a user approved a permission request, the request was auto-denied:
the agent was told its request was "denied" and the request was removed from the
pending inbox. Now a failed approval is reported as `FAILED` instead: the request
stays pending (no response event is written, the agent is not notified), and the
desktop dialog shows the failure reason so the user can click Approve again to
retry. Denials remain a separate, explicit user action.
