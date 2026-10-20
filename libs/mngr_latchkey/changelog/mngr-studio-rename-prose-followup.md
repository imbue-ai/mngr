Review follow-up to the Imbue Studio prose sweep.

The sweep had changed the OAuth and WebDAV prose in `core.py`, `discovery.py`
and `permission_requests.mjs` but not the tests and README that repeat it, so
one project named the product both ways. `README.md`'s account-scope section,
`core_test.py`'s OAuth-client prose, `services_catalog_test.py`'s scope comment
and `custom_services_test.py`'s catalog comment now say Imbue Studio too.

Ten hyphenated compounds are reworded. "Imbue Studio-provided client" parses
as "Imbue [Studio-provided]" now that the name is two words, so these follow
the form the product docs already use: "the client Imbue Studio provides", "the
redirect URI Imbue Studio hosts", "the endpoints Imbue Studio owns".

This project's sweep entry claimed `minds-api-proxy`, `minds-workspaces` and
`MINDS_GOOGLE_OAUTH_SERVICES` were "being handled separately". They are
identifiers and keep their `minds` spelling for good, per
`apps/minds/style_guide.md`; the entry now says so, and its list of what the
sweep touched is filled out.
