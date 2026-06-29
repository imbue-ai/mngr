The container realizer can now run a base image that is already present in the VPS's local Docker daemon, skipping both the build and the pull.

`RealizePlacementContext` gained an opt-in `allow_local_image` flag (off by default, so OVH/vultr/aws behavior is unchanged); when set and the image is already loaded locally, `realize_placement` runs it as-is. This backs the imbue_cloud slice "build once per box, `docker load` per slice" acceleration.
