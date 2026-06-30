`ImbueCloudProvider.rename_host` is now implemented: a leased host can be renamed by updating its mutable `host_name` via the connector. The lease's `host_db_id` remains the durable identity, so a rename never touches the VPS or container and works whether or not the container is running.

The pre-baked pool host is no longer stamped with a `workspace=<name>` label at bake time; workspace identity lives on the host name and host id, not on a label.
