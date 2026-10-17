Fixed: GitHub and GitLab connections are now labelled "GitHub" and "GitLab" instead of "GitHub (REST API)" and "GitLab (REST API)".

Both services expose several latchkey scopes (GitHub: REST, GraphQL, and git), and one stored credential backs all of them, so a single connection covers every scope. The workspace Permissions tab, the Connectors settings page, Add connection, and the messages of the dialog that takes a service's credentials were labelling that whole connection with its *first* scope's name -- which read as though GraphQL and git access sat beneath a "REST API" permission, when the three are separate and independently grantable.

The per-scope labels are unchanged where they identify one scope: the dividers inside a connection's panel and the permission-request dialog still read "GitHub (REST API)", "GitHub (GraphQL API)", and "GitHub (git)".
