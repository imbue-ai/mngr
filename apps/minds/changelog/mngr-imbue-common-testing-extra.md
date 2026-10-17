`scripts/build_test.py` gains an acceptance test that installs the built `imbue-common` wheel into a
scratch venv: plain, it must pull in no pytest; with `[testing]`, its test library must import.
