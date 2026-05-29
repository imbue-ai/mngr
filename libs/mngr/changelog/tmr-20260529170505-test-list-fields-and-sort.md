Fixed the LISTING tutorial example for choosing fields and sort order: it
referenced a non-existent agent field `created_at` and used an unsupported `-`
prefix for descending sort. The example now uses the real field `create_time`
and the documented `create_time desc` sort syntax.

Strengthened the corresponding e2e release test (`test_list_fields_and_sort`)
to create real Modal agents and assert that the selected fields render (including
a non-empty `create_time` timestamp and `host.provider` of `modal`) and that
`create_time desc` orders newest-first.
