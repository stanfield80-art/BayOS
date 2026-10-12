# BayOS Runner

The Runner is one signed Shortcut. It runs approved recipes. The gateway key is not inside the shortcut file.

A recipe stays a draft until you tap Approve on the tablet page. Posting a note, the ChatGPT plugin, and the Runner itself cannot approve a recipe.

## Install the shortcut

1. Deploy the gateway with `deploy.ps1` if it is not already deployed. The script prints `BayOS Runner shortcut:` and a URL.
2. On the iPhone, open Safari and go to `https://YOUR-GATEWAY/shortcuts/BayOS-Runner.shortcut`. Replace `YOUR-GATEWAY` with the host the script printed.
3. Open the file and add the shortcut named BayOS Runner. The file is signed with HubSign. It has no gateway key and no gateway address.
4. Run BayOS Runner once by hand.
5. When it asks for the gateway address, paste the host from the deploy script, including `https://`, with no trailing slash. Example shape: `https://bayos-gateway-xxxxx.a.run.app`
6. When it asks for the gateway key, paste the same key you use on the tablet page.
7. The shortcut saves a JSON object `{"url","key"}` in the Shortcuts iCloud folder as `BayOS-Runner.txt`. That file is a secret. Anyone who can open that iCloud folder can read the key. If the file is missing or is not that JSON, the shortcut deletes it and asks again. Delete `BayOS-Runner.txt` and run the shortcut again to forget the key. An older copy that stored the address and key on two lines is not valid. The next run deletes it and asks once more.

Asking for the key on every run would be safer, and it would also stop the 9:00 AM automation, because nobody would be there to type it. The file is the choice that lets the morning run proceed.

## Approve daily-health

The gateway creates a draft recipe named `daily-health` the first time the recipe list is read. It does not run until you approve it.

1. On the iPhone, open `https://YOUR-GATEWAY/app`.
2. Enter the gateway key if the page asks. The key stays in that browser.
3. Tap **Recipes**.
4. Open **daily-health**. Read the plain-English summary and the full JSON.
5. Tap **Approve**. The page sends the gateway key with that request.

The summary says it collects sleep hours, steps, resting heart rate, and blood oxygen minimum and average, then posts them to the hub health log. The suggested time is 9:00 AM daily.

Approving locks the id, name, version, type, params, and schedule hint. The hub stores a SHA-256 hash of that canonical JSON. The summary line is only for reading. It is not part of the hash.

## One automation per recipe

1. Open Shortcuts, then the **Automation** tab.
2. Tap the plus, then **Time of Day** (or **Create Personal Automation**, then **Time of Day**).
3. Set 9:00 AM and repeat Daily.
4. Choose **Run Shortcut**, then **BayOS Runner**.
5. Set the input to `daily-health`. The shortcut uses that name and does not show a menu.
6. Turn off **Ask Before Running** if iOS shows that switch.
7. If iOS still asks you to tap Run, that is the system confirmation for a shortcut that sends data off the phone. The gateway cannot turn that prompt off.

Repeat those steps for each later recipe. Change the time to match that recipe's schedule hint, and set the input to that recipe's name. Example: a recipe named `morning-note` uses input `morning-note`.

If you run BayOS Runner with no input, it shows a menu of approved recipe names.

## What each recipe type does

The hub rejects any type or field that is not on this list.

- `health_daily` reads Apple Health for the fields in the recipe and posts them to `/api/v1/health/daily`. The morning seed uses sleep hours, steps, resting heart rate, SpO2 minimum, and SpO2 average. The first run asks for Health access. Allow Sleep, Steps, Resting Heart Rate, and Blood Oxygen. Then check the tablet for a note titled `Daily health` plus today's date. If sleep hours look like a huge number, or blood oxygen looks like `0.97` instead of `97`, run it once more after the shortcut's unit fix (seconds or minutes become hours, and a fraction at or below 1 becomes a percent). If a health step returns nothing, open the shortcut and set that Find Health Samples action to **Start Date is in the last 1 day**.
- `calendar_read` asks the hub for the next N days. The secret calendar link stays on the server. The shortcut posts the hub's reply to the inbox. Set the calendar link in `deploy.ps1` first, or the reply says no calendar is configured.
- `post_note` posts the approved title, body, and tags to the inbox as agent `shortcut-001`.
- `notify` shows a notification on the phone, then tells the hub that it did.
- `reminder_add` adds a reminder on the phone, then tells the hub. The due text is a date the Shortcuts date action can read, such as `tomorrow at 9:00 AM`.
- `open_url` asks you to confirm, then opens an `https` link. The host must be on `BAYOS_OPEN_URL_HOSTS` (comma-separated names, no IP addresses). The seed recipe is not an open-url recipe. `deploy.ps1` replaces the service env vars, so add `BAYOS_OPEN_URL_HOSTS=example.com` to the `$setEnv` line in `deploy.ps1` before you deploy, or the next deploy will drop a value you set only in the console.
- `save_vault_note` posts the approved title and body to the hub with the tag `vault`.

The shortcut asks before `open_url`, because that leaves the phone for a site that is not the hub. Posts to the hub do not ask again inside the shortcut. Health, calendar, notes, and the result log are hub posts.

Every run also posts a result note from agent `shortcut-001` with the tag `runner`.

## Create another draft

`POST /api/v1/runner/recipes` with header `X-BayOS-Key` creates a draft. It cannot set `status` to approved. Example:

```json
{"name":"morning-note","version":1,"type":"notify","params":{"text":"Four walls first."},"schedule_hint":"8:00 AM daily"}
```

Approve it on the tablet page, then add an automation whose input is `morning-note`.
