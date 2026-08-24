# Forecast AI Mesh Relay

Private Railway service for Forecast AI to call approved MeshGateway merchants from server-side agent workflows. It is **disabled by default** and is not exposed to browsers.

## Why a separate service

MeshGateway's official paid-client library is TypeScript. Forecast AI's analysis backend is Python. This small Node service isolates the payment key and only accepts requests from Forecast's private backend network.

It does not accept caller-supplied URLs, paths, or authentication headers. Every payable destination must be explicitly allowlisted in `MESH_MERCHANTS_JSON`.

## Railway variables

Keep all of these only in the Mesh Relay service, never in the frontend:

```ini
MESH_ENABLED=false
MESH_RELAY_API_KEY=<long random internal key>
MESH_BUYER_PRIVATE_KEY=<dedicated low-balance Mesh payment wallet>
MESH_MAX_AMOUNT_PER_REQUEST=0.05
MESH_DAILY_BUDGET_USDG=1.00
MESH_MERCHANTS_JSON={}
```

When a merchant is selected, the JSON will look like:

```json
{
  "provider-id": {
    "label": "Provider display name",
    "endpoint": "https://api.meshgateway.co/m/provider-slug/v1/offer",
    "maxAmount": 0.03
  }
}
```

`maxAmount` is a per-call upper bound and may not exceed `MESH_MAX_AMOUNT_PER_REQUEST`.

## Activation checklist

1. Select and inspect the exact merchant OpenAPI document.
2. Add only that exact paid endpoint to the allowlist.
3. Use a new dedicated wallet containing a small amount of USDG on Robinhood Chain. Never use the deployer, proof publisher, owner or personal wallet.
4. Add the private key directly in Railway. Do not commit or send it in chat.
5. Complete the wallet's one-time Permit2 approval with the official Mesh client.
6. Replace the in-memory spending guard with the planned Supabase payment ledger before user-facing production activation.
7. Enable `MESH_ENABLED=true` only after a single audited test call succeeds.

## Current scope

The relay is scaffolding only. It does not yet route a Forecast analysis through Mesh and does not change the standard Forecast seven-agent swarm. That happens after the first merchant contract and response format are verified.
