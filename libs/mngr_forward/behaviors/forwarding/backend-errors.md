# Backend errors: timing and the stdout side channel

## What the client observes versus what the timing is

The scenarios in `backend-errors.feature` state only the status a requesting client observes.
Two timing facts sit behind them and are easy to misread.

The proxy does not cap a request's total duration: a backend that keeps answering, however slowly, is never cut off, because a cap there would break any agent endpoint that legitimately runs long.
What bounds a silent backend is a per-read timeout on the gap between bytes, not a whole-request deadline; a buffered request whose backend accepts and then goes silent trips that timeout and resolves to the 504 in `wedged-backend-504`.

Separately, and without touching the request, a backend that has not answered a buffered request within a short window causes the proxy to emit an advisory "STALLED" event on its stdout stream.
That event only enrolls the agent for active probing; it never abandons the request.

## The stdout side channel

Backend failures also surface as machine-readable events on the proxy's stdout stream, which an embedding consumer drives its recovery from rather than the HTTP responses its users never see directly.
That stream decomposes a failure -- a connect error, an exhausted pool, a backend that listens but never answers -- into reasons a consumer can act on, where the HTTP status only says retry, lost, or timed out.

That stdout contract is deliberately left unspecified in this corpus for now; it belongs to a planned stream area covering the whole envelope stream.
Until that area lands, the payload shapes in `imbue/mngr_forward/data_types.py` are the reference for the stream.
