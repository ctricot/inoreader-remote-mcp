# inoreader-remote-mcp

A remote [MCP](https://modelcontextprotocol.io) server for [Inoreader](https://www.inoreader.com):
read, mark and manage your feeds from Claude — web, desktop and mobile — through a custom
connector. Self-hosted, single user, one Docker container, no database.

> **Not affiliated with Inoreader.** This is an independent project using the public
> Inoreader API. Inoreader is a trademark of its owner.

## What it does

Ten tools, each costing **one** Inoreader API call (Inoreader allows about a hundred
requests per day per zone on the Pro plan):

| Tool | Returns |
|---|---|
| `get_user_info` | The authenticated Inoreader user |
| `get_unread_counts` | Unread counts: total, per folder, per feed |
| `get_subscriptions` | Subscriptions: id, title, feed URL, site URL, folders |
| `get_folders_and_tags` | Folders and tags, with unread counts |
| `get_articles` | Articles of a feed, a folder or the whole account (`stream_id`, `unread_only`, `limit` ≤ 100, `since`, `continuation`) |
| `get_starred_articles` | Starred articles (`limit`, `continuation`) |
| `add_subscription` | Subscribe to a feed, optionally into a folder and with a title (a second call only then) |
| `remove_subscription` | Unsubscribe — Claude is told to ask for confirmation first |
| `mark_as_read` / `mark_as_unread` | Mark a list of articles, in a single call |

Articles come back short: id, title, URL, feed name, publication date, `crawlTimeMsec` and a
plain-text summary cut at 500 characters. Pagination is handed to Claude: a page carries a
`continuation` value, passed back for the next page. When a daily quota is reached, Claude
receives an explicit message with the time until the counters reset.

Tool descriptions are written in French.

## How it works

```
Claude ──HTTPS──▶ reverse proxy ──▶ inoreader-remote-mcp :8000 ──▶ Inoreader API
                                     ├── /mcp            MCP, Streamable HTTP, bearer token
                                     ├── OAuth 2.1 server for Claude (/register, /authorize, /token…)
                                     ├── /oauth/consent  consent screen (admin password)
                                     └── /oauth/inoreader/start|callback  one-time Inoreader authorisation
```

Two OAuth flows, independent:

- **Claude → this server.** Claude's custom connectors require OAuth. The server is its own
  authorisation server: Claude registers itself, you approve on a consent screen with the
  admin password, Claude gets an access token (1 h) and a rotating refresh token (60 days).
- **This server → Inoreader.** Done once, in your browser. The Inoreader tokens are stored
  on the volume and refreshed automatically.

Everything persistent is two JSON files in `DATA_DIR`, written atomically with `0600`
permissions: `inoreader_tokens.json` and `oauth.json` (Claude's clients and token digests —
tokens are never stored in clear).

## Requirements

- An Inoreader account with API access (Inoreader Pro, see their developer terms).
- A host running Docker, reachable over **HTTPS** under a public hostname (Claude connects
  from the internet), behind a reverse proxy that terminates TLS.
- Claude with custom connectors available on your plan.

## 1. Register an Inoreader app

In Inoreader: **Preferences → Developer → Register an application** (use a dedicated app
for this server, so that its quota is not shared with other tools).

- **Redirect URI**: `https://inoreader.example.com/oauth/inoreader/callback`
  (your public hostname, exactly).
- **Scope**: Read and write.

Note the **App ID** and **App Key**.

## 2. Configure

Copy [`.env.example`](.env.example) to `.env` next to `docker-compose.yml`, then
`chmod 600 .env`:

| Variable | Description |
|---|---|
| `PUBLIC_URL` | Public HTTPS address, no trailing slash, e.g. `https://inoreader.example.com` |
| `INOREADER_APP_ID` | Inoreader App ID |
| `INOREADER_APP_KEY` | Inoreader App Key |
| `ADMIN_PASSWORD` | Protects the Inoreader authorisation and the Claude consent screen. Use a long random value: `head -c 32 /dev/urandom \| base64` |
| `LOG_LEVEL` | `INFO` by default |
| `DATA_DIR` | Set to `/data` by the image; the token files live there |

Left empty, `ADMIN_PASSWORD` closes the admin routes rather than opening them.

## 3. Deploy

```sh
mkdir -p data          # before the first start, so that it belongs to you, not root
docker compose pull
docker compose up -d
docker compose logs -f # expect: Uvicorn running on http://0.0.0.0:8000
```

The provided [`docker-compose.yml`](docker-compose.yml) publishes **no port**: the container
joins an external Docker network named `proxy`, shared with the reverse proxy. Adjust it to
your setup. The container runs as UID 1000 / GID 100.

Reverse proxy: forward `https://inoreader.example.com` to `http://inoreader-remote-mcp:8000`.
MCP responses are streamed, so disable response buffering. With nginx (or Nginx Proxy
Manager, *Advanced* tab):

```nginx
proxy_buffering off;
proxy_read_timeout 300s;
```

Check: `curl https://inoreader.example.com/health` returns `{"status":"ok",…}`.

## 4. Authorise Inoreader (once)

Open `https://inoreader.example.com/oauth/inoreader/start` in a browser. Log in with any
user name and the `ADMIN_PASSWORD`, approve on Inoreader, and you land on « Inoreader est
connecté ». `data/inoreader_tokens.json` now exists.

To bind another Inoreader account, or after revoking the app in Inoreader, run it again.

## 5. Add the connector in Claude

In Claude: **Settings → Connectors → Add custom connector**.

- **Name**: Inoreader
- **URL**: `https://inoreader.example.com/mcp`
- Leave the advanced OAuth client fields empty: Claude registers itself.

Click **Connect**. The consent screen shows the client name and **where the code will be
sent** — it must be `claude.ai`; refuse otherwise. Type the admin password and approve. The
connector is then available on web, desktop and mobile.

Try: « Combien d'articles non lus ai-je sur Inoreader ? »

## Updating

The CI publishes `ghcr.io/ctricot/inoreader-remote-mcp:latest` and `:sha-<commit>` on every
push to `main`.

- **Automatic**: the compose file carries the label
  `com.centurylinklabs.watchtower.enable=true`; a Watchtower running with
  `WATCHTOWER_LABEL_ENABLE=true` pulls each new image.
- **Manual**: `docker compose pull && docker compose up -d`.
- **Roll back**: `IMAGE_TAG=sha-<commit> docker compose up -d`.

Tokens survive updates: Claude stays connected, Inoreader stays authorised.

## Security notes

- The admin password is the only key: it authorises Inoreader and approves Claude clients.
  Wrong attempts are slowed down; still, use a long random value.
- The consent password is typed into a form, never sent as HTTP Basic, so a forged request
  from another site cannot approve a client.
- `data/` holds the Inoreader refresh token: keep it private and back it up. Deleting
  `data/oauth.json` disconnects every Claude client.

## Development

```sh
uv sync
uv run pytest       # Inoreader is mocked with respx, no network
uv run ruff check && uv run ruff format --check
ADMIN_PASSWORD=dev uv run python -m inoreader_remote_mcp   # http://localhost:8000
```

## License

[MIT](LICENSE)
