Strengthened the two-space-indent assertions in `antigravity_config_test.py`. The
previous `assert "  " in serialized` checks could not distinguish two-space from
four-space (or wider) indentation, so they did not actually verify the format the
serializers promise. They now assert that a top-level key line begins with exactly
two spaces.
