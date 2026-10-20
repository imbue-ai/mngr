# Service-name pattern made public

`SERVICE_NAME_PATTERN` in `additional_services.py` (previously private) is now
the documented canonical service-name shape. The minds embed contract's
wire-side service-name check mirrors it, and a new alignment test in
apps/minds (`test_service_name_alignment.py`) fails CI if the two rules drift.
No behavior change.
