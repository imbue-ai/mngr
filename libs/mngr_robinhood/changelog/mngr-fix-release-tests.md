Fixed the robinhood streaming release tests (`test_streaming.py`), which drive a real
claude agent in tmux. They were missing `@pytest.mark.tmux`, so the resource-guard PATH
wrapper blocked their tmux usage and the robinhood subprocess exited 2. Added the mark
(plus a longer per-test timeout, since a real agent run far exceeds the default 30s).
Test-only change; robinhood's streaming behavior is unchanged.
