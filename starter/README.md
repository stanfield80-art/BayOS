# BayOS Starter

A starting point for a person who wants their own BayOS in ChatGPT. It is not tied to anyone else's notes, vault, or gateway.

Each person gets their own data:

- Their own Custom GPT, using the instructions in `BayOS-Starter-GPT-instructions.md`.
- Their own fill-in profile, written in that file or kept in a private folder. Do not commit the filled-in profile to a shared repository.
- Optionally, their own Cloud Run gateway. That gateway has its own inbox and its own gateway key. It does not read another person's Firestore database.

## Use it in ChatGPT only

1. Copy `BayOS-Starter-GPT-instructions.md`.
2. Replace every `[FILL IN]` line with that person's own facts. Delete any line they do not want the GPT to know.
3. In ChatGPT, choose **GPTs**, then **Create**, then the **Configure** tab.
4. Set the name, description, and instructions from that file.
5. Save the GPT. Do not add an Action unless they have deployed their own gateway and have read `gpt/BayOS-Arm-GPT.md` in their own copy of this repository.

This path stores nothing on a server you do not already use. ChatGPT keeps the GPT configuration in that person's OpenAI account.

## Optional: their own gateway

Skip this unless they want an inbox outside the chat.

1. They need their own Google account, a Google Cloud project with billing, and the Google Cloud CLI.
2. They clone this repository into a folder on their own computer.
3. They read `DEPLOY.md` in that clone and run `deploy.ps1` from that folder.
4. The script asks for their project id and their own gateway key. It does not reuse another person's key, project, or calendar link.
5. Their tablet page is the URL the script prints, plus `/app`. Their action schema is that URL plus `/gpt/openapi.json`.
6. They can then follow `gpt/BayOS-Arm-GPT.md` with their own URL and their own key.

`deploy.ps1` creates their Firestore database and their Secret Manager secrets in their project. Notes posted to their gateway stay in their project. Sharing a gateway key shares that inbox. Do not send the key in email or chat.
