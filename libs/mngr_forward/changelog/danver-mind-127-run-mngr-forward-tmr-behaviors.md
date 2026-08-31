Lightened rot-prone documentation across `mngr_forward`, so module docstrings and comments no longer make claims that go stale as the code moves underneath them.

Several docstrings pointed readers at an "acceptance test" for coverage they said was not in the file. No such test exists for this project, so those references sent readers looking for something that was never there. `stream_manager_test.py`, `cli_test.py` and `server_test.py` now describe what they cover, without claiming what they do not.

Other corrections: `resolver.py` documented a `resolve()` failure mode that does not exist (missing SSH info returns a target, not `None`) and omitted the discovery-membership state that gates every lookup; `server.py`'s route list said "only handles" while omitting `/_bridge`; `data_types.py` referred to "the three reasons below" where seven now follow; `stream_manager.py` and `embedding.py` cited a class and a documentation path that have since moved.

Docstrings describing the live routing path in terms of the legacy `host-<hex>` coordinate now say `agent-<hex>`, matching the origin scheme the proxy actually serves. Deliberate references to the legacy coordinate are unchanged.
