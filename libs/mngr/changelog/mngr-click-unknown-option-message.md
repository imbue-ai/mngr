`mngr create` with an unrecognized option is still rejected exactly as before; only the release test that checks it changed.

The test required click's usage error to read `No such option: --flag`, but click now words it `No such option '--flag'.`, so the test failed on every platform against current dependencies. It now checks that the error names the rejected option without pinning click's punctuation, which is what the test was always meant to prove.
