Marked `TestingModalInterface` with `__test__ = False` so pytest no longer
attempts to collect it as a test class (its name starts with "Test"). This
silences the "cannot collect test class ... because it has a __init__
constructor" warning in CI. No behavior change.
