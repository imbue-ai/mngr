Fixed the LABELS-AND-FILTERING e2e test `test_list_filter_by_label_cel`: removed
the superfluous `@pytest.mark.modal` mark. A bare `mngr list` only does
discovery-only across providers and never shells out to the `modal` CLI, so the
resource guard could not see Modal being invoked and failed the test's Modal mark.
Also strengthened the test to create labeled agents and assert that the CEL
`--include 'labels.priority == "high"'` filter actually selects the matching agent
and excludes non-matching ones.
