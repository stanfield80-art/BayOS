#Requires -Version 5.1
<#
.SYNOPSIS
  Deploy the BayOS gateway to Google Cloud Run.

  Signs in if needed, selects a project, enables APIs, stores
  BAYOS_AUTH_KEY and GEMINI_API_KEY in Secret Manager without echoing
  them, deploys from this folder, then smoke-tests /health and ingest.
#>

$ErrorActionPreference = "Stop"
# PowerShell 7 can turn gcloud's stderr into a terminating error. Judge success by exit code.
if (Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
    $PSNativeCommandUseErrorActionPreference = $false
}

$Region = "us-central1"
$ServiceName = "bayos-gateway"
$MinInstances = "0"
$MaxInstances = "1"
$AuthSecretName = "bayos-auth-key"
$GeminiSecretName = "gemini-api-key"

function Invoke-Gcloud {
    param([Parameter(Mandatory = $true)][string[]]$GcloudArgs)
    & gcloud @GcloudArgs
    if ($LASTEXITCODE -ne 0) {
        throw "This command failed: gcloud $($GcloudArgs -join ' ')"
    }
}

function Read-HiddenValue {
    param([Parameter(Mandatory = $true)][string]$Prompt)
    $secure = Read-Host -AsSecureString -Prompt $Prompt
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
}

function Save-SecretValue {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Value
    )
    $path = Join-Path $env:TEMP ("bayos-" + [Guid]::NewGuid().ToString("n") + ".bin")
    $utf8 = New-Object System.Text.UTF8Encoding $false
    try {
        [System.IO.File]::WriteAllText($path, $Value, $utf8)
        & gcloud @("secrets", "describe", $Name, "--format=value(name)") 1>$null 2>$null
        $exists = ($LASTEXITCODE -eq 0)
        if ($exists) {
            Write-Host "Secret $Name already exists. Adding a new version."
            & gcloud @("secrets", "versions", "add", $Name, "--data-file", $path, "--quiet")
        } else {
            Write-Host "Creating secret $Name."
            & gcloud @(
                "secrets", "create", $Name,
                "--data-file", $path,
                "--replication-policy", "automatic",
                "--quiet"
            )
        }
        if ($LASTEXITCODE -ne 0) {
            throw "Could not store secret $Name in Secret Manager."
        }
        Write-Host "Stored secret $Name."
    } finally {
        if (Test-Path -LiteralPath $path) {
            $blank = " " * 64
            [System.IO.File]::WriteAllText($path, $blank, $utf8)
            Remove-Item -LiteralPath $path -Force
        }
    }
}

function Invoke-GatewayRequest {
    param(
        [Parameter(Mandatory = $true)][string]$Method,
        [Parameter(Mandatory = $true)][string]$Uri,
        [hashtable]$Headers,
        [string]$JsonBody
    )
    $params = @{
        Method     = $Method
        Uri        = $Uri
        TimeoutSec = 180
    }
    if ($Headers) {
        $params["Headers"] = $Headers
    }
    if ($JsonBody) {
        $params["ContentType"] = "application/json"
        $params["Body"] = [System.Text.Encoding]::UTF8.GetBytes($JsonBody)
    }
    try {
        return Invoke-RestMethod @params
    } catch {
        $detail = $_.Exception.Message
        if ($_.ErrorDetails -and $_.ErrorDetails.Message) {
            $detail = $_.ErrorDetails.Message
        }
        throw "Call to $Uri failed. $detail"
    }
}

$AuthKey = $null
$GeminiKey = $null

try {
    Set-Location -Path $PSScriptRoot

    if (-not (Test-Path -LiteralPath ".\main.py")) {
        throw "main.py was not found. Run this script from the BayOS folder."
    }
    if (-not (Test-Path -LiteralPath ".\Dockerfile")) {
        throw "Dockerfile was not found next to this script."
    }

    $gcloudCmd = Get-Command gcloud -ErrorAction SilentlyContinue
    if (-not $gcloudCmd) {
        Write-Host "The Google Cloud CLI is not installed, or this window was opened before it was installed."
        Write-Host "Install it with:"
        Write-Host "  winget install --id Google.CloudSDK -e"
        Write-Host "Then close this window, open a new PowerShell window, cd to this folder, and run .\deploy.ps1 again."
        exit 1
    }

    Write-Host ""
    Write-Host "BayOS Cloud Run deploy"
    Write-Host "Region: $Region    Service: $ServiceName    Min instances: $MinInstances"
    Write-Host ""
    Write-Host "The project needs billing turned on before this will succeed."
    Write-Host "If you have not done that, press Ctrl+C and open https://console.cloud.google.com/billing"
    Write-Host ""

    Write-Host "Step 1 of 6: sign in to Google Cloud"
    $activeAccount = & gcloud auth list --filter="status:ACTIVE" --format="value(account)"
    if ($LASTEXITCODE -ne 0) {
        throw "gcloud auth list failed. Close this window, open a new one, and try again."
    }
    if ([string]::IsNullOrWhiteSpace($activeAccount)) {
        Write-Host "A browser window will open so you can sign in."
        & gcloud auth login
        if ($LASTEXITCODE -ne 0) {
            throw "Sign-in did not finish."
        }
    } else {
        Write-Host "Already signed in as $activeAccount"
        Write-Host "To use a different Google account, press Ctrl+C and run: gcloud auth login"
    }

    Write-Host ""
    Write-Host "Step 2 of 6: choose the Google Cloud project"
    $ProjectId = Read-Host "Project ID (the short id, not the display name)"
    if ([string]::IsNullOrWhiteSpace($ProjectId)) {
        throw "A project ID is required."
    }
    $ProjectId = $ProjectId.Trim()
    Invoke-Gcloud @("config", "set", "project", $ProjectId)
    & gcloud projects describe $ProjectId --format="value(projectId)" 1>$null
    if ($LASTEXITCODE -ne 0) {
        throw "Project '$ProjectId' was not found. Create one at https://console.cloud.google.com/projectcreate and turn on billing, then run .\deploy.ps1 again."
    }

    Write-Host ""
    Write-Host "Step 3 of 6: turn on Cloud Run, Secret Manager, Artifact Registry, and Cloud Build"
    Write-Host "This can take a few minutes the first time."
    Invoke-Gcloud @(
        "services", "enable",
        "run.googleapis.com",
        "secretmanager.googleapis.com",
        "artifactregistry.googleapis.com",
        "cloudbuild.googleapis.com",
        "--quiet"
    )

    Write-Host ""
    Write-Host "Step 4 of 6: store the two keys in Secret Manager"
    Write-Host "What you paste is hidden. It is not printed, not put on the command line,"
    Write-Host "and not written into the container image."
    $AuthKey = Read-HiddenValue -Prompt "BayOS gateway key (BAYOS_AUTH_KEY)"
    if ([string]::IsNullOrWhiteSpace($AuthKey)) {
        throw "The BayOS gateway key was empty."
    }
    $AuthKey = $AuthKey.Trim()
    $GeminiKey = Read-HiddenValue -Prompt "Gemini API key (GEMINI_API_KEY)"
    if ([string]::IsNullOrWhiteSpace($GeminiKey)) {
        throw "The Gemini API key was empty."
    }
    $GeminiKey = $GeminiKey.Trim()
    Save-SecretValue -Name $AuthSecretName -Value $AuthKey
    Save-SecretValue -Name $GeminiSecretName -Value $GeminiKey

    Write-Host ""
    Write-Host "Step 5 of 6: let Cloud Run read those secrets"
    $projectNumber = & gcloud projects describe $ProjectId --format="value(projectNumber)"
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($projectNumber)) {
        throw "Could not read the project number for $ProjectId."
    }
    $projectNumber = "$projectNumber".Trim()
    $RuntimeSa = "$projectNumber-compute@developer.gserviceaccount.com"
    Write-Host "Service account: $RuntimeSa"
    Write-Host "Granting Cloud Run Builder so Cloud Build can deploy. The first grant can take a couple of minutes to spread."
    Invoke-Gcloud @(
        "projects", "add-iam-policy-binding", $ProjectId,
        "--member", "serviceAccount:$RuntimeSa",
        "--role", "roles/run.builder",
        "--quiet"
    )
    foreach ($secretName in @($AuthSecretName, $GeminiSecretName)) {
        Invoke-Gcloud @(
            "secrets", "add-iam-policy-binding", $secretName,
            "--member", "serviceAccount:$RuntimeSa",
            "--role", "roles/secretmanager.secretAccessor",
            "--quiet"
        )
    }

    Write-Host ""
    Write-Host "Step 6 of 6: deploy bayos-gateway from this folder"
    Write-Host "The first build often takes several minutes."
    $setSecrets = "BAYOS_AUTH_KEY=${AuthSecretName}:latest,GEMINI_API_KEY=${GeminiSecretName}:latest"
    Invoke-Gcloud @(
        "run", "deploy", $ServiceName,
        "--source", ".",
        "--region", $Region,
        "--platform", "managed",
        "--min-instances", $MinInstances,
        "--max-instances", $MaxInstances,
        "--memory", "512Mi",
        "--allow-unauthenticated",
        "--service-account", $RuntimeSa,
        "--set-secrets", $setSecrets,
        "--quiet"
    )

    $serviceUrl = & gcloud run services describe $ServiceName --region $Region --format="value(status.url)"
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($serviceUrl)) {
        throw "The deploy command finished, but the service URL could not be read."
    }
    $serviceUrl = "$serviceUrl".Trim().TrimEnd("/")

    Write-Host ""
    Write-Host "Smoke test: /health"
    $health = $null
    $attempt = 0
    while ($attempt -lt 6) {
        $attempt++
        try {
            $health = Invoke-GatewayRequest -Method "Get" -Uri ($serviceUrl + "/health")
            break
        } catch {
            if ($attempt -ge 6) {
                throw
            }
            Write-Host "The service is still starting. Waiting 10 seconds..."
            Start-Sleep -Seconds 10
        }
    }
    Write-Host ("Health: " + ($health.status) + ", version " + ($health.version))

    Write-Host "Smoke test: authenticated ingest, then inbox"
    $marker = "Reply with the single word pong."
    $json = @{ task = $marker; agent = "prof-x-001" } | ConvertTo-Json -Compress
    $headers = @{ "X-BayOS-Key" = $AuthKey }
    $ingest = Invoke-GatewayRequest -Method "Post" -Uri ($serviceUrl + "/api/v1/task/ingest") -Headers $headers -JsonBody $json
    Write-Host ("Ingest status: " + $ingest.status)
    Write-Host ("Ingest output: " + $ingest.output)

    $inbox = Invoke-GatewayRequest -Method "Get" -Uri ($serviceUrl + "/api/v1/inbox?limit=5") -Headers $headers
    $found = $false
    foreach ($item in @($inbox.items)) {
        if ($null -ne $item -and $item.task -eq $marker) {
            $found = $true
        }
    }
    if (-not $found) {
        throw "Ingest succeeded, but the inbox did not list the smoke-test message."
    }
    Write-Host "Inbox listed the smoke-test message."
    Write-Host $inbox.notice

    Write-Host ""
    Write-Host "Deployed: $serviceUrl"
    Write-Host ""
    Write-Host "Use the hub from this window:"
    Write-Host ('  $env:BAYOS_URL = "' + $serviceUrl + '"')
    Write-Host '  $env:BAYOS_AUTH_KEY = "<the BayOS gateway key you just entered>"'
    Write-Host "  .\bayos.ps1 inbox"
    Write-Host '  .\bayos.ps1 send "text" --from prof-x-001'
    Write-Host ""
    Write-Host "The gateway key was not printed. Keep it in your password manager."
} finally {
    $AuthKey = $null
    $GeminiKey = $null
}
