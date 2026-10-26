# Remote multi-account deployment (Railway)

This fork adds a hosted, read-only, multi-account mode on top of upstream
[googleanalytics/google-analytics-mcp](https://github.com/googleanalytics/google-analytics-mcp).
Upstream's `analytics_mcp` package is untouched; everything here lives in `remote-server/` and
`.github/workflows/sync-upstream.yml`, so upstream releases merge cleanly. (The folder name has a
hyphen on purpose: it keeps setuptools' flat-layout discovery from treating it as a package.)

| Piece | What it does |
|---|---|
| `remote-server/server.py` | Loads one Google login per account, routes each call to the account that can see the `property_id`, merges `get_account_summaries` across accounts, exposes only read-only tools, serves Streamable HTTP behind a shared secret |
| `remote-server/login.py` | Run locally once per Google account to create a read-only login |
| `remote-server/Dockerfile` | Railway image. Service settings live in Railway: `RAILWAY_DOCKERFILE_PATH=remote-server/Dockerfile`, health check `/health` |
| `sync-upstream.yml` | Daily: merge upstream, run all tests, push to `main` only if green (Railway redeploys); opens an issue on failure |

## 1. Google Cloud (once)

Reuses the `gsc-mcp` Google Cloud project and its Desktop OAuth client.

1. Enable the [Analytics Admin API](https://console.cloud.google.com/apis/library/analyticsadmin.googleapis.com) and
   [Analytics Data API](https://console.cloud.google.com/apis/library/analyticsdata.googleapis.com) in that project.
2. The OAuth app must be **published** (Audience → Publish app), otherwise logins expire after 7 days.

## 2. Sign in each Google account (once per account)

```bash
uv run --with google-auth-oauthlib python remote-server/login.py --label acct1 --client-secrets ~/.config/gsc-mcp/client_secret.json
```

Pick the account in the browser, click through "Google hasn't verified this app"
(Advanced → Go to …), and allow read-only Analytics access. Each run saves
`remote-server/tokens/<label>.json` (gitignored) and prints the GA4 properties it can see.

## 3. Railway variables

| Variable | Value |
|---|---|
| `MCP_AUTH_TOKEN` | Random secret, ≥32 chars |
| `GA_TOKEN_<LABEL>` | Contents of `remote-server/tokens/<label>.json`, one per account |
| `RAILWAY_DOCKERFILE_PATH` | `remote-server/Dockerfile` |
| `MCP_ALLOW_URL_TOKEN` | Optional, default `true`. Set `false` to require the header and disable `/<token>/mcp` |

```bash
railway variables --set "GA_TOKEN_ACCT1=$(cat remote-server/tokens/acct1.json)"
```

## 4. Connect clients

Endpoint: `https://<your-app>.up.railway.app/mcp`, header `Authorization: Bearer <MCP_AUTH_TOKEN>`.
Clients that can't send headers can use `https://<your-app>.up.railway.app/<MCP_AUTH_TOKEN>/mcp`.

## Operations

- **Add/replace an account:** rerun `login.py` with a label, set its variable. Railway redeploys.
- **Rotate the secret:** change `MCP_AUTH_TOKEN`, update clients.
- **Revoke a login:** <https://myaccount.google.com/permissions> → remove the app.
- **Tests:** `uv run --with pytest pytest remote-server/test_server.py -q`
