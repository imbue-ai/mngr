Removed the incorrect `@pytest.mark.modal` from the `test_list_with_no_agents`
e2e test. With no agents, `mngr list` cannot create the Modal environment and
short-circuits the Modal provider load via the SDK (without shelling out to the
`modal` CLI), so the resource guard's CLI PATH wrapper never observes a Modal
invocation and the mark tripped the "marked with modal but never invoked modal"
check. Added an explanatory comment so the mark is not re-added.
