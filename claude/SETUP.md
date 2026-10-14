# Connect BayOS to Claude

Claude custom connectors use the same gateway MCP server as ChatGPT: `https://YOUR-GATEWAY/mcp`. Replace `YOUR-GATEWAY` with the host `deploy.ps1` printed. Claude signs in with OAuth 2.1. It does not send the gateway key as a header. The consent page on the gateway asks for that key.

Choose **Register automatically** under **OAuth client**. Do not choose **Use Claude's published identity**. That option is a Client ID Metadata Document, and this gateway does not advertise one. Leave request headers empty. The gateway key is typed on the BayOS page.

## Click path

These labels are from Anthropic's help article [Get started with custom connectors using remote MCP](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp).

### Pro and Max

1. Deploy the gateway and copy the MCP URL, including `/mcp`.
2. Open **Customize**, then **Connectors**.
3. Select **+ Add**, then **Add custom connector**.
4. Enter the name `BayOS`. That name is what you see in the connector list. It is not the OAuth client name.
5. Paste the MCP URL and select **Continue**.
6. Review the authentication settings Claude detected and select **Continue**.
7. Under **Authentication**, choose **Sign in now**. **Sign in when needed** also works. Do not choose **No sign in**.
8. Under **OAuth client**, choose **Register automatically**.
9. Leave **Request headers** empty.
10. Select **Add**.
11. When the BayOS consent page opens, enter the gateway key and select **Allow inbox access**. The page names the registered client and the redirect host. The key stays on the gateway.

### Team and Enterprise

An Owner or Primary Owner adds the connector before members can use it. Enterprise also allows a custom role with Manage access to Libraries.

1. Open **Organization settings**, then **Connectors** (`https://claude.ai/admin-settings/connectors`).
2. Select **Add**.
3. Hover **Custom**, then select **Web**.
4. Enter the name `BayOS`, paste the MCP URL, and select **Continue**.
5. Under **Authentication**, choose **Sign in now**.
6. Under **OAuth client**, choose **Register automatically**.
7. Leave **Request headers** empty and select **Add**.
8. Each member then connects that connector and enters the gateway key on the BayOS consent page.

Free accounts can add one custom connector and use the same URL and **Register automatically**.

## Callbacks the gateway accepts

Hosted Claude apps (claude.ai, Desktop, mobile, and Cowork) must use an exact redirect URI. These two are allowed, with no user info, query, fragment, trailing slash, or non-443 port:

- `https://claude.ai/api/mcp/auth_callback`
- `https://claude.com/api/mcp/auth_callback`

Anthropic's connector authentication page documents the first URL as the fixed callback for those hosted apps: [Authentication for connectors](https://claude.com/docs/connectors/building/authentication#callback-urls). The same page does not list a separate `claude.com` callback. This gateway allows that second exact URL as well, because custom connectors have used it. Lookalike hosts are rejected.

The same page documents Claude Code's loopback redirect. The gateway also allows `http://localhost/callback` and `http://127.0.0.1/callback` on any port, which is the shape Claude Code sends (for example `http://localhost:3118/callback`). The path must be `/callback`. Other local paths are rejected unless `BAYOS_OAUTH_ALLOW_LOCAL=1`, which is for tests. The consent page warns when the redirect host is this computer.

## What gets stored

Dynamic client registration is `POST /oauth/register`. The server keeps the `client_name` Claude sends. If that name contains the word Claude, such as `Claude`, `Claude Desktop`, or `claude.ai`, notes use agent `claude-001`. A name that only shares those letters, such as `claudette`, does not. Any other name keeps agent `chatgpt-001`, or the registered agent in `BAYOS_MCP_AGENT` when that variable is set. The registered name is always stored on the note as `client_name`.

Inbox text from `claude-001` is untrusted. Tool results start with `UNTRUSTED INBOX DATA`.

## What the server requires

- Transport: streamable HTTP at `/mcp`. One JSON-RPC message per POST.
- Discovery: `/.well-known/oauth-protected-resource/mcp` and `/.well-known/oauth-authorization-server`.
- Auth: authorization code with PKCE `S256`. `token_endpoint_auth_method` must be `none`. Scope is `bayos`. The resource is the MCP URL. The authorization response includes `iss`.
- Access tokens last 15 minutes. Refresh tokens last 30 days and rotate on use. The gateway stores only hashes.
- Tools: `list_inbox`, `post_note`, `mark_read`, and `get_calendar` only when a calendar link is configured.
