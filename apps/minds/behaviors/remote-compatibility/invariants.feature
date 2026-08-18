Feature: Remote-compatibility invariants
  Properties that hold whenever the desktop client talks to an imbue cloud server newer than itself.

  @unknown-response-fields-ignored
  Rule: A response field the client does not recognize never breaks the operation
    Any imbue cloud response may carry fields added after this client shipped; the client ignores them and the operation completes as if they were absent.
    Removing or renaming a field the client requires still fails the operation loudly.

  @unknown-enum-values-not-actionable
  Rule: An enumerated wire value the client does not recognize degrades to "shown but not actionable"
    A workspace whose lifecycle status is unrecognized is displayed with its state unknown; state-changing operations on it are refused with a message naming the remedy (update the app), and it is never treated as absent, stopped, or destroyed.

    @unknown-status-start-refused
    Example: Starting a workspace in an unrecognized state is refused with the update remedy
      Given a remote workspace whose lifecycle status this client does not recognize
      When the user asks to start the workspace
      Then the request is refused with a message telling the user to update the app

  @unreadable-listing-never-empty
  Rule: A listing the client cannot read is an error, never an empty fleet
    When every entry of a non-empty remote listing fails to parse, the client reports the listing as failed; it never presents zero workspaces, because downstream logic cannot distinguish that from a genuinely empty account.
    A single unreadable entry is skipped with a warning while the rest of the listing survives.

  @newer-records-read-only
  Rule: A workspace record too new for this client is read-only here
    The record and its workspace remain visible, and connecting to the workspace remains possible, but every operation that would change the record's meaning -- pushing changes, destroying, releasing, or removing the record -- is refused with a message telling the user to update the app.
    The server independently refuses pushes that carry a record format below the stored row's.

    @newer-record-push-refused
    Example: A push against a newer-format record is refused
      Given a synced workspace record whose record format exceeds what this client understands
      When the client attempts to push a change to that record
      Then the push is refused with a message telling the user to update the app

  @newer-payloads-never-rewritten
  Rule: A secrets blob too new for this client is never rewritten by it
    A client that cannot interpret a blob's payload format leaves the stored blob untouched, and when a client does rewrite a blob it round-trips payload keys it does not recognize verbatim, so material a newer client stored is never silently dropped.

  @absent-fields-preserved-on-push
  Rule: A record field absent from a push keeps its stored value
    The server merges record pushes preserve-on-absent: a field the pushing client never named keeps its stored value, while an explicitly sent null clears it, so an older client's push cannot reset a field it does not know about.
