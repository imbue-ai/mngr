- `AzureProviderConfig` gained optional service-principal fields (`client_id` / `tenant_id` / `client_secret`): when all are set, a `ClientSecretCredential` is used instead of `DefaultAzureCredential` (which remains the unchanged default). Used by the Minds bring-your-own-account paste flow.

- Resource-provider registration timeout raised 180s → 900s: a fresh subscription's first-ever `Microsoft.Network` registration routinely exceeds 3 minutes (observed live); registration is one-time per subscription.
