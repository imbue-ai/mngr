Added two framework hooks used by test map-reduce:

- `--reducer-branch-suffix` appends a suffix to the reducer's branch, agent, and host names. A reintegration reuses the original run's name (to rediscover its mappers by label) but must open its pull request from a branch that does not collide with the original run's reducer branch; the suffix keeps them distinct.

- A new `on_all_mappers_finalized` recipe hook fires once every mapper has finished, just before the reducer launches, with the full mapper set including failures. A recipe whose reducer reads the output directory as files can use it to reconcile the two views -- e.g. represent a failed mapper that produced no output so it is not silently absent.
