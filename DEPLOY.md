# Deploy BayOS on Google Cloud Run

This puts the BayOS gateway on Google Cloud Run so it has a web address that stays deployed. You run everything from Windows PowerShell on your PC. Docker is not required. Google builds the container from the `Dockerfile` in this folder.

The gateway checks a private key on every send and inbox call. That key lives in Google Secret Manager and is handed to Cloud Run as `BAYOS_AUTH_KEY`. It is not written into the container image, and it is not stored in git. `.env` is listed in `.gitignore`.

Gemini is called through Vertex AI as the Cloud Run service account. No Gemini API key is stored. AI Studio keys that start with `AQ.` are rejected by the Generative Language API with `401 ACCESS_TOKEN_TYPE_UNSUPPORTED` even when the full key is sent in `x-goog-api-key`.

The service is deployed with a minimum of 0 instances in `us-central1` (Iowa). The address stays. When nobody is calling it, Google shuts the running copy down so you are not paying for an idle server. The first call after a quiet period can take several seconds while a new copy starts.

## Commands, in order

Open PowerShell and run these once:

```powershell
winget install --id Google.CloudSDK -e
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned -Force
```

Close that window so the new `gcloud` command is picked up. Open a new PowerShell window and run:

```powershell
cd C:\Users\Joseph\path\to\BayOS
.\deploy.ps1
```

Change the `cd` path to the folder that contains `deploy.ps1`. The script signs you in, asks for the project id, turns the APIs on, asks for the BayOS gateway key without showing it, deploys, then calls `/health` and an authenticated ingest.

When it finishes it prints a URL. In that same window, or a new one, run:

```powershell
$env:BAYOS_URL = "https://bayos-gateway-xxxxx-uc.a.run.app"
$env:BAYOS_AUTH_KEY = "the same gateway key you pasted into deploy.ps1"

Invoke-RestMethod -Uri "$env:BAYOS_URL/health"

.\bayos.ps1 send "Reply with the single word pong." --from prof-x-001
.\bayos.ps1 inbox
```

Replace the URL with the one `deploy.ps1` printed. To make the shorter name `bayos` work in that window:

```powershell
$env:Path += ";$PWD"
bayos inbox
bayos send "text" --from prof-x-001
```

## Before you start

1. A Google account.
2. A Cloud project you own. Create one at https://console.cloud.google.com/projectcreate if you do not have one. The project ID is the short name, such as `bayos-hub`, not the display name.
3. Billing turned on for that project: https://console.cloud.google.com/billing . Cloud Run can sit at $0 while it is idle, but Google still requires a billing account before the first deploy.
4. This BayOS folder on the PC (a git clone or a downloaded copy).
5. A long random string you invent as the BayOS gateway key. Keep it in a password manager. Anyone who has it can call the gateway. You do not need a Gemini API key.

## What `deploy.ps1` does

You only paste into the prompts. The blocks below are the commands the script runs, so you can see them. You do not need to paste those yourself.

### 1. Install the Google Cloud CLI

```powershell
winget install --id Google.CloudSDK -e
```

If `winget` is not available, download the Windows installer from https://cloud.google.com/sdk/docs/install and run it. Then open a **new** PowerShell window.

### 2. Allow your scripts to run

Windows blocks scripts until you allow them for your user:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned -Force
```

That applies only to your Windows user. If the company PC refuses the change, run the deploy this way instead:

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy.ps1
```

### 3. Go to the BayOS folder

```powershell
cd C:\Users\Joseph\path\to\BayOS
```

### 4. Sign in

The script runs this if you are not already signed in. A browser window opens.

```powershell
gcloud auth login
```

### 5. Set the project

The script asks for the project ID, then runs:

```powershell
gcloud config set project YOUR_PROJECT_ID
```

### 6. Turn on the APIs

```powershell
gcloud services enable run.googleapis.com secretmanager.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com aiplatform.googleapis.com
```

- **Cloud Run** hosts the gateway.
- **Secret Manager** holds the BayOS gateway key.
- **Artifact Registry** stores the built container.
- **Cloud Build** builds that container from this folder.
- **Vertex AI** (`aiplatform.googleapis.com`) is how the service calls Gemini.

The first time, this step can take a few minutes.

### 7. Create the gateway secret

The script asks once. Nothing you type is shown.

The BayOS gateway key is stored as the secret `bayos-auth-key`, then exposed to the service as `BAYOS_AUTH_KEY`.

The value goes to Secret Manager through a temporary file that the script deletes. It is not placed on the `gcloud` command line, where it would be visible in history. If you run `deploy.ps1` again and paste a new value, a new secret version is added. Cloud Run uses the `latest` version.

Do not put these keys in a `.env` file, the Dockerfile, or a command you copy into chat.

### 8. Let Cloud Run read the secret and call Vertex AI

The script grants the project's Compute Engine service account three roles:

- `roles/run.builder`, so Cloud Build can build and deploy.
- `roles/secretmanager.secretAccessor` on `bayos-auth-key`, so the running service can read the gateway key.
- `roles/aiplatform.user`, so that same service account can call Gemini on Vertex AI.

The first grant can take a couple of minutes to spread. If the deploy or the ingest smoke test says permission denied, wait two minutes and run `.\deploy.ps1` again. Paste the same gateway key when it asks.

### 9. Deploy

```powershell
gcloud run deploy bayos-gateway --source . --region us-central1 --platform managed --min-instances 0 --max-instances 1 --memory 512Mi --allow-unauthenticated --service-account PROJECT_NUMBER-compute@developer.gserviceaccount.com --set-secrets BAYOS_AUTH_KEY=bayos-auth-key:latest --set-env-vars GOOGLE_CLOUD_PROJECT=YOUR_PROJECT_ID,GOOGLE_CLOUD_LOCATION=global,BAYOS_GEMINI_BACKEND=vertex --quiet
```

- `--source .` uploads this folder and builds the `Dockerfile`. The upload skips `.env` and the vault folders.
- `--region us-central1` is the Iowa region. It is a normal default and is on the Cloud Run free tier.
- `--min-instances 0` shuts the copy down when it is idle. The URL stays.
- `--max-instances 1` keeps a single copy, so the in-memory inbox is not split across two copies. It still clears when the copy shuts down. See below.
- `--allow-unauthenticated` opens Google's front door so PowerShell can call the URL without a second Google login. Send and inbox still require the `X-BayOS-Key` header. `/health` is public and does not include the keys.
- `--set-secrets` mounts the gateway key as an environment variable. It is not baked into the image.
- `--set-env-vars` points Gemini at Vertex AI in this project. `GOOGLE_CLOUD_LOCATION=global` is required for `gemini-3.8-flash`. That model is not served from `us-central1`. The Cloud Run service itself still runs in Iowa.

Leave `BAYOS_INFERENCE_MODE` unset. The service then calls Vertex AI. The value `stub` skips Gemini and is only for a local test. Set `BAYOS_GEMINI_BACKEND=apikey` and `GEMINI_API_KEY` only if you intentionally want the old Generative Language API path.

The first build often takes several minutes.

### 10. Smoke test from PowerShell

`deploy.ps1` does this for you at the end. To repeat it by hand:

```powershell
$env:BAYOS_URL = "https://bayos-gateway-xxxxx-uc.a.run.app"
$env:BAYOS_AUTH_KEY = "the same gateway key you pasted into deploy.ps1"

Invoke-RestMethod -Uri "$env:BAYOS_URL/health"

$headers = @{ "X-BayOS-Key" = $env:BAYOS_AUTH_KEY }
$json = '{"task":"Reply with the single word pong.","agent":"prof-x-001"}'
$bytes = [System.Text.Encoding]::UTF8.GetBytes($json)
Invoke-RestMethod -Method Post -Uri "$env:BAYOS_URL/api/v1/task/ingest" -Headers $headers -ContentType "application/json" -Body $bytes

Invoke-RestMethod -Uri "$env:BAYOS_URL/api/v1/inbox" -Headers $headers
```

`/health` should show `"status": "healthy"`, `"gemini_backend": "vertex"`, `"gemini_location": "global"`, and `"gemini_model": "gemini-3.8-flash"`. The ingest call should show `"status": "completed"` and a short reply. The inbox call should list that message.

The same inbox check through the helper:

```powershell
.\bayos.ps1 inbox
.\bayos.ps1 send "text" --from prof-x-001
```

`--from` is the agent id. `prof-x-001` is the default. Other ids, such as `pony-001` or `chronos-001`, are listed in `CLAUDE.md`.

## Inbox memory

`GET /api/v1/inbox` (what `bayos inbox` calls) lists recent tasks stored in the running instance's memory. With minimum instances set to 0, Google stops that instance after it has been idle. The next call starts a fresh instance and the list is empty. That is expected with this deploy.

A cheap way to keep the list is Cloud Firestore in Native mode. The free tier is enough for a personal hub. It is not built yet. When you want it:

1. Enable `firestore.googleapis.com` and create a Native-mode database in the same project.
2. Grant the Cloud Run service account the role **Cloud Datastore User** (`roles/datastore.user`).
3. Change the gateway so each ingest writes a document in a collection named `inbox`, and `GET /api/v1/inbox` reads the newest documents from there.

Until that exists, treat the inbox as a live view of the current instance, not as a permanent archive.

## If something fails

| What you see | What to do |
| --- | --- |
| `gcloud` is not recognized | Close PowerShell, open a new window, and try again. |
| `Invalid choice: 'secrets describe ...'` | Pull the latest copy of this folder, then run `powershell -ExecutionPolicy Bypass -File .\deploy.ps1` again. If the secrets already exist, the script adds a new version. |
| Scripts are disabled | Run the execution-policy command in the section above. |
| Project not found, or billing | Create the project, turn on billing, then run `.\deploy.ps1` again. |
| Permission denied on the first deploy | Wait two minutes and run `.\deploy.ps1` again. |
| Ingest returns permission denied, `403`, or a Vertex AI error | Wait two minutes so `roles/aiplatform.user` can spread, then run `powershell -ExecutionPolicy Bypass -File .\deploy.ps1` again. Billing must be on for this project. |
| Ingest still mentions `ACCESS_TOKEN_TYPE_UNSUPPORTED` | That is the old Generative Language API key path. `/health` should say `"gemini_backend": "vertex"`. Pull the latest copy and run the deploy script again. |
| Inbox is empty but you sent something earlier | The instance scaled to zero and the memory inbox was cleared. Send again and read the inbox while it is still warm. |
