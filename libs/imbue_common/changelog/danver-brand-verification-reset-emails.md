The trailing-comments ratchet no longer counts a hex colour that closes a string literal (`assert "background: #e9ecd9" in body`) as a comment. Its hex-colour exemption accepted a colour followed by `;` or whitespace only; a closing quote now counts as well.

Unit tests for the shared regex rules live in `ratchet_testing/common_ratchets_test.py`. Their fixtures hold the anti-patterns the rules look for, so that file joins `test_ratchets.py` and `standard_ratchet_checks.py` in the files every regex ratchet skips.
