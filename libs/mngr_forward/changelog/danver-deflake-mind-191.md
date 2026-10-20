Deflaked `test_subdomain_forward_emits_no_stall_envelope_when_the_backend_answers_in_time`, and dropped the `@pytest.mark.flaky` marker it had been carrying.

It used to race a real 50ms stall timer against the request in wall-clock time, so under CI scheduling jitter the timer could fire before the handler disarmed it and a spurious `STALLED` envelope failed the test. It now runs on an event loop whose clock only advances when the test says so: the request runs to completion (disarming the timer) while the clock is frozen, and only then is the clock moved past the stall deadline. The check is a deterministic fact about the disarm rather than a timing race, and it needs no retries.

No production behavior changed -- the handler always did cancel the timer, and the real stall window is 30s.
