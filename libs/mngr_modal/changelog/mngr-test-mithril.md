Deflake the Modal discover-hosts and restart acceptance tests: Modal's sandbox listing is eventually consistent, so the tests now poll discovery instead of asserting on a single snapshot.
