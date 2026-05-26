## mngr

- e2e: fixed `test_create_with_label` in `libs/mngr/imbue/mngr/e2e/test_basic.py`. Removed the incorrect `@pytest.mark.modal` (mngr is invoked as a subprocess and uses the Modal Python SDK in-process, so the test runner's CLI/SDK guards never observe Modal usage and `pytest.mark.modal` fires `RESOURCE GUARD: ... never invoked modal`). Added `@pytest.mark.timeout(120)` to match other e2e tests; the default 10s pytest-timeout is too tight for the create + list flow.
