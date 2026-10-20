# BayOS Starter

A plugin template for a person who wants their own BayOS in ChatGPT. It is not tied to anyone else's notes, vault, or gateway.

Each person gets their own data:

- Their own plugin, using `plugin.json` and `skills/bayos-starter/SKILL.md`.
- Their own fill-in profile, written in that skill or kept in a private folder. Do not commit the filled-in profile to a shared repository.
- Optionally, their own Cloud Run gateway. That gateway has its own inbox, its own gateway key, and its own OAuth tokens. It does not read another person's Firestore database.

## Use the plugin

1. Copy this folder.
2. Replace every `[FILL IN]` line in `skills/bayos-starter/SKILL.md` with that person's own facts. Delete any line they do not want the assistant to know.
3. If they are not running a gateway, delete `mcp.json` before packaging. A skill-only plugin does not connect to an inbox.
4. In ChatGPT, open [Plugins](https://chatgpt.com/plugins). Install Plugin Creator if asked, or select **Add (+)** and describe this plugin. If **Upload plugin** is available, zip this folder so `plugin.json` is at the top of the zip and upload that file.
5. Mention the plugin with `@` in a new chat.

`BayOS-Starter-GPT-instructions.md` is the old Custom GPT text. It is deprecated and works only until Dec 11, 2026.

## Optional: their own gateway

Skip this unless they want an inbox outside the chat.

1. They need their own Google account, a Google Cloud project with billing, and the Google Cloud CLI.
2. They clone this repository into a folder on their own computer.
3. They read `DEPLOY.md` in that clone and run `deploy.ps1` from that folder.
4. The script asks for their project id and their own gateway key. It does not reuse another person's key, project, or calendar link.
5. They replace `gateway.example` in `mcp.json` with the host the script prints. The MCP path is `/mcp`.
6. They follow `chatgpt-plugin/SETUP.md` with that URL. The consent page asks for their own gateway key.

`deploy.ps1` creates their Firestore database and their Secret Manager secrets in their project. Notes posted to their gateway stay in their project. Sharing a gateway key shares that inbox. Do not send the key in email or chat.
