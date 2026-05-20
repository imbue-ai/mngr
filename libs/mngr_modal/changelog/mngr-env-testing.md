- `mngr_modal`: Modal backend now raises a new `ProviderEmptyError`
  (distinct from `ProviderUnavailableError`) when its per-user
  environment doesn't exist yet, so `mngr list` can silently skip the
  empty provider instead of aborting. (Counterpart to the new
  `ProviderEmptyError` handling in the listing pipeline.)
