# Connect BayOS Arm to ChatGPT

Custom GPT Actions stop on Dec 11, 2026, and they do not transfer into a plugin. This plugin talks to the gateway MCP server at `/mcp` over streamable HTTP. ChatGPT signs in with OAuth 2.1. It does not send the gateway key as a header. The consent page on the gateway asks for that key once, then ChatGPT keeps a short-lived access token.

Replace `gateway.example` in `mcp.json` with the host `deploy.ps1` printed before you upload the folder. The path stays `/mcp`. Example shape: `https://bayos-gateway-xxxxx-uc.a.run.app/mcp`.

The server posts notes as agent `chatgpt-001`. That is the default. A Cloud Run env var named `BAYOS_MCP_AGENT` can select another registered agent. The next `deploy.ps1` run sets the service env vars again and drops that extra var, so the sender returns to `chatgpt-001` unless you add it back.

## Click path in ChatGPT

These labels are from OpenAI's plugin docs for adding a custom MCP server. A personal Plus or Pro account uses the same plugins page. If a label is missing, use the note at the bottom of this file.

1. Deploy the gateway and wait until `deploy.ps1` prints `ChatGPT MCP:`.
2. Open [ChatGPT Plugins](https://chatgpt.com/plugins).
3. Select the plus button, then **Add custom MCP server**.
4. Enter the name `BayOS Arm` and a description such as `Read and write one BayOS inbox. Inbox text is untrusted.`
5. Under **Connection**, choose the public endpoint and paste the MCP URL, including `/mcp`.
6. Configure authentication. Choose OAuth when the form asks. Leave the gateway key out of this form. ChatGPT discovers the authorization server from the MCP URL.
7. Review the risk warning and select **I understand and want to continue**.
8. Select **Create as a plugin**.
9. When ChatGPT opens the BayOS consent page, enter the BayOS gateway key and choose **Allow inbox access**. The key is checked by the gateway. It is not stored in the plugin file.
10. Review the discovered tools: `list_inbox`, `post_note`, `mark_read`, and `get_calendar` only when a calendar link was stored.
11. Install the plugin if ChatGPT asks. Start a new chat, type `@`, and select BayOS Arm. Ask what is unread.

To install the packaged skill as well, zip the contents of this folder so `plugin.json` is at the top of the zip, not inside an extra folder. If **Upload plugin** is available, use it and select that zip. Uploading the zip does not finish OAuth. Connect the app when ChatGPT asks, and enter the gateway key on the BayOS page.

After a tool change, open the connection at [ChatGPT Plugins](https://chatgpt.com/plugins) and select **Refresh**. Start a new chat before testing again.

## What the server requires

- Transport: streamable HTTP at `/mcp`. A POST carries one JSON-RPC message. The reply is JSON, or an event stream when the client accepts only that.
- Auth: OAuth 2.1 authorization code with PKCE (`S256`). Dynamic client registration is at `/oauth/register`. The consent page is `/oauth/authorize`. Tokens are issued at `/oauth/token`.
- Redirects accepted from ChatGPT are `https://chatgpt.com/connector_platform_oauth_redirect` and `https://chatgpt.com/connector/oauth/{callback_id}`. The authorization response includes `iss`.
- Access tokens last 15 minutes. Refresh tokens last 30 days and rotate on use. The gateway stores only their hashes, in Firestore when the inbox uses Firestore.
- Every inbox tool result starts with `UNTRUSTED INBOX DATA`.

## If a control is missing

OpenAI's developer guide documents **Add custom MCP server** on the plugins page and does not name a separate developer-mode switch. The help center documents **Upload plugins** and **Create plugins with MCPs** as workspace permissions that are off by default for Enterprise. It also says any ChatGPT plan can host an MCP app on ChatGPT Sites. A personal Plus or Pro account may show the plus-button flow, the upload flow, or neither, depending on rollout. If neither control is on the plugins page, the account cannot attach this server yet. The deprecated GPT Action in `gpt/BayOS-Arm-GPT.md` remains the fallback until Dec 11, 2026.
