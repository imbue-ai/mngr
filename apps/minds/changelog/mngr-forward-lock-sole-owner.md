Nothing in the desktop client reads the `mngr latchkey forward` supervisor's legacy `latchkey_forward.json` record, which no longer exists.

The wait-for-gateway-port loop already asked the ownership lock who owns the latchkey directory; this drops the last references to the record's type from its documentation.
