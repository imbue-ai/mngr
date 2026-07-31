The description a user writes in the in-app "Report a bug" flow is no longer thrown away in transit.

Sentry's default data scrubber was replacing the whole description with `[Filtered]` whenever it contained a substring like `auth`, `secret`, or `password` anywhere in the text -- which ordinary prose does. Roughly one in nine reports reached us with nothing in it; in one case the word "authored" destroyed a 9,705-character report.

The report collector now passes the description explicitly to the Sentry submitter, which uploads a verbatim copy to the same S3 bucket the log attachments already go to and records its URI on the event. Uploads are not scrubbed, so the body is recoverable even when the inline copy comes back `[Filtered]`. As with the log attachments, this needs a configured bucket -- `production` and `staging` have one, `development` does not.

Each submitted report now lands as its own issue in Sentry rather than all of them stacking into one, so a report can be triaged, assigned and resolved on its own.
