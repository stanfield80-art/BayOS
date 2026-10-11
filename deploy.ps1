#Requires -Version 5.1
<#
.SYNOPSIS
  Deploy the BayOS gateway to Google Cloud Run.

  Signs in if needed, selects a project, enables APIs, stores
  BAYOS_AUTH_KEY in Secret Manager without echoing it, grants the
  Cloud Run service account Vertex AI User and Cloud Datastore User,
  creates a Native Firestore database when one is missing, deploys from
  this folder, then smoke-tests /health, a direct inbox post, and ingest.
  Gemini is called with that service account. No Gemini API key is required.
  Inbox items are stored in Firestore collection inbox.
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
$VertexLocation = "global"
$FirestoreLocation = "nam5"

$script:GcloudExecutable = $null

function Resolve-GcloudExecutable {
    # Windows PowerShell resolves `gcloud` to gcloud.ps1. That shim builds
    # $run_args_array and then runs `& "$exe_path" $run_args_array` without
    # splatting. On Windows PowerShell 5.1 the array is joined into one
    # string, so python sees a single invalid choice. gcloud.cmd skips the shim.
    $cmd = Get-Command -Name "gcloud.cmd" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($cmd -and $cmd.Source) {
        return [string]$cmd.Source
    }

    $found = Get-Command -Name "gcloud" -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $found) {
        return $null
    }

    $source = [string]$found.Source
    if (-not $source -and $found.Path) {
        $source = [string]$found.Path
    }
    if ($source -match '(?i)\.ps1$') {
        $sibling = Join-Path (Split-Path -Parent $source) "gcloud.cmd"
        if (Test-Path -LiteralPath $sibling) {
            return (Resolve-Path -LiteralPath $sibling).Path
        }
        throw "Found gcloud.ps1 but not gcloud.cmd beside it. Reinstall the Google Cloud CLI, then open a new window."
    }
    return $source
}

function Initialize-GcloudExecutable {
    if ($script:GcloudExecutable) {
        return $script:GcloudExecutable
    }
    $script:GcloudExecutable = Resolve-GcloudExecutable
    return $script:GcloudExecutable
}

function Invoke-GcloudNative {
    param(
        [Parameter(Mandatory = $true)][string[]]$ArgumentList,
        [switch]$Quiet
    )
    if (-not (Initialize-GcloudExecutable)) {
        throw "The Google Cloud CLI is not installed."
    }

    $previousEap = $ErrorActionPreference
    # Stop would turn python.exe stderr into a terminating NativeCommandError
    # before LASTEXITCODE can be read. Continue still shows real gcloud errors.
    if ($Quiet) {
        $ErrorActionPreference = "SilentlyContinue"
    } else {
        $ErrorActionPreference = "Continue"
    }
    $restoreNative = $false
    $previousNative = $false
    if (Test-Path -Path "variable:PSNativeCommandUseErrorActionPreference") {
        $restoreNative = $true
        $previousNative = $PSNativeCommandUseErrorActionPreference
        $PSNativeCommandUseErrorActionPreference = $false
    }

    $code = 1
    $captured = $null
    try {
        # Do not add a redirection operator on this call. On Windows PowerShell 5.1,
        # redirection plus a splatted array collapses every argument into one string.
        $captured = & $script:GcloudExecutable @ArgumentList
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousEap
        if ($restoreNative) {
            $PSNativeCommandUseErrorActionPreference = $previousNative
        }
    }
    if ($null -eq $code) {
        $code = 1
    }
    $text = ""
    if ($null -ne $captured) {
        $text = (@($captured) | ForEach-Object { "$_" }) -join "`n"
        $text = $text.Trim()
    }
    if (-not $Quiet -and $text) {
        Write-Host $text
    }
    return [pscustomobject]@{
        ExitCode = [int]$code
        Output   = $text
    }
}

function Test-GcloudSecretExists {
    param([Parameter(Mandatory = $true)][string]$Name)
    if (-not (Initialize-GcloudExecutable)) {
        throw "The Google Cloud CLI is not installed."
    }
    $previousEap = $ErrorActionPreference
    # A missing secret writes ERROR to stderr. Under Stop, Windows PowerShell 5.1
    # turns that into a terminating NativeCommandError. SilentlyContinue does not.
    $ErrorActionPreference = "SilentlyContinue"
    $restoreNative = $false
    $previousNative = $false
    if (Test-Path -Path "variable:PSNativeCommandUseErrorActionPreference") {
        $restoreNative = $true
        $previousNative = $PSNativeCommandUseErrorActionPreference
        $PSNativeCommandUseErrorActionPreference = $false
    }
    $code = 1
    try {
        & $script:GcloudExecutable secrets describe $Name --format=json 2>$null | Out-Null
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousEap
        if ($restoreNative) {
            $PSNativeCommandUseErrorActionPreference = $previousNative
        }
    }
    if ($null -eq $code) {
        $code = 1
    }
    return ($code -eq 0)
}

function Invoke-Gcloud {
    param([Parameter(Mandatory = $true)][string[]]$ArgumentList)
    $result = Invoke-GcloudNative -ArgumentList $ArgumentList
    if ($result.ExitCode -ne 0) {
        throw "This command failed: gcloud $($ArgumentList -join ' ')"
    }
    return $result
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
    $path = Join-Path ([System.IO.Path]::GetTempPath()) ("bayos-" + [Guid]::NewGuid().ToString("n") + ".bin")
    $utf8 = New-Object System.Text.UTF8Encoding $false
    try {
        [System.IO.File]::WriteAllText($path, $Value, $utf8)
        # Pass each argument separately. A splatted array plus 2>$null is what
        # made Windows PowerShell 5.1 hand gcloud.ps1 one joined string.
        $exists = Test-GcloudSecretExists -Name $Name
        if ($exists) {
            Write-Host "Secret $Name already exists. Adding a new version."
            $null = Invoke-Gcloud -ArgumentList @(
                "secrets", "versions", "add", $Name, "--data-file", $path, "--quiet"
            )
        } else {
            Write-Host "Creating secret $Name."
            $created = Invoke-GcloudNative -ArgumentList @(
                "secrets", "create", $Name,
                "--data-file", $path,
                "--replication-policy", "automatic",
                "--quiet"
            )
            if ($created.ExitCode -ne 0) {
                Write-Host "Create did not succeed. Adding a new version in case the secret already exists."
                $null = Invoke-Gcloud -ArgumentList @(
                    "secrets", "versions", "add", $Name, "--data-file", $path, "--quiet"
                )
            }
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

function Get-FirestoreDatabase {
    # (default) is the database id. Single quotes keep PowerShell from
    # treating the parentheses as a subexpression.
    $described = Invoke-GcloudNative -Quiet -ArgumentList @(
        "firestore", "databases", "describe",
        '--database=(default)',
        "--format=json"
    )
    if ($described.ExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($described.Output)) {
        return $null
    }
    $info = $described.Output | ConvertFrom-Json
    $dbType = [string]$info.type
    if ([string]::IsNullOrWhiteSpace($dbType)) {
        $dbType = [string]$info.databaseType
    }
    $location = [string]$info.locationId
    if ([string]::IsNullOrWhiteSpace($location)) {
        $location = [string]$info.location
    }
    return [pscustomobject]@{
        Type     = $dbType
        Location = $location
    }
}

function Ensure-FirestoreDatabase {
    $existing = Get-FirestoreDatabase
    if ($null -ne $existing) {
        if ($existing.Type -and $existing.Type -notmatch 'NATIVE') {
            throw "Firestore database '(default)' is type '$($existing.Type)' in '$($existing.Location)'. BayOS needs Native mode. Use a project whose default database is Native, then run .\deploy.ps1 again."
        }
        Write-Host "Firestore database '(default)' already exists in $($existing.Location)."
        return
    }

    Write-Host "Creating a Native Firestore database in $FirestoreLocation (United States, includes Iowa)."
    $created = Invoke-GcloudNative -ArgumentList @(
        "firestore", "databases", "create",
        '--database=(default)',
        "--location=$FirestoreLocation",
        "--type=firestore-native",
        "--edition=standard",
        "--quiet"
    )
    if ($created.ExitCode -eq 0) {
        Write-Host "Firestore database created."
        return
    }

    Write-Host "Create did not succeed. Checking whether the database already exists."
    Start-Sleep -Seconds 15
    $existing = Get-FirestoreDatabase
    if ($null -eq $existing) {
        throw "Could not create the Firestore database. Turn on billing, then run .\deploy.ps1 again."
    }
    if ($existing.Type -and $existing.Type -notmatch 'NATIVE') {
        throw "Firestore database '(default)' is type '$($existing.Type)' in '$($existing.Location)'. BayOS needs Native mode."
    }
    Write-Host "Firestore database '(default)' is present in $($existing.Location)."
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

# Dot-sourcing loads the functions for tests. `powershell -File .\deploy.ps1` still runs the deploy.
if ($MyInvocation.InvocationName -ne '.') {

$AuthKey = $null

try {
    Set-Location -Path $PSScriptRoot

    if (-not (Test-Path -LiteralPath ".\main.py")) {
        throw "main.py was not found. Run this script from the BayOS folder."
    }
    if (-not (Test-Path -LiteralPath ".\Dockerfile")) {
        throw "Dockerfile was not found next to this script."
    }

    $gcloudCmd = Resolve-GcloudExecutable
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
    $authList = Invoke-Gcloud -ArgumentList @("auth", "list", "--filter=status:ACTIVE", "--format=json")
    $activeAccount = ""
    if ($authList.Output) {
        $accounts = @($authList.Output | ConvertFrom-Json)
        $activeAccount = ($accounts | ForEach-Object { [string]$_.account }) -join ", "
    }
    if ([string]::IsNullOrWhiteSpace($activeAccount)) {
        Write-Host "A browser window will open so you can sign in."
        $null = Invoke-Gcloud -ArgumentList @("auth", "login")
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
    $null = Invoke-Gcloud -ArgumentList @("config", "set", "project", $ProjectId)
    $projectCheck = Invoke-GcloudNative -Quiet -ArgumentList @("projects", "describe", $ProjectId, "--format=json")
    if ($projectCheck.ExitCode -ne 0) {
        throw "Project '$ProjectId' was not found. Create one at https://console.cloud.google.com/projectcreate and turn on billing, then run .\deploy.ps1 again."
    }

    Write-Host ""
    Write-Host "Step 3 of 6: turn on Cloud Run, Secret Manager, Artifact Registry, Cloud Build, Vertex AI, and Firestore"
    Write-Host "This can take a few minutes the first time."
    $null = Invoke-Gcloud -ArgumentList @(
        "services", "enable",
        "run.googleapis.com",
        "secretmanager.googleapis.com",
        "artifactregistry.googleapis.com",
        "cloudbuild.googleapis.com",
        "aiplatform.googleapis.com",
        "firestore.googleapis.com",
        "--quiet"
    )

    Write-Host ""
    Write-Host "Step 4 of 6: store the BayOS gateway key in Secret Manager"
    Write-Host "What you paste is hidden. It is not printed, not put on the command line,"
    Write-Host "and not written into the container image."
    Write-Host "Gemini does not use an API key. Cloud Run calls Vertex AI as this project's service account."
    $AuthKey = Read-HiddenValue -Prompt "BayOS gateway key (BAYOS_AUTH_KEY)"
    if ([string]::IsNullOrWhiteSpace($AuthKey)) {
        throw "The BayOS gateway key was empty."
    }
    $AuthKey = $AuthKey.Trim()
    Save-SecretValue -Name $AuthSecretName -Value $AuthKey

    Write-Host ""
    Write-Host "Step 5 of 6: let Cloud Run call Vertex AI and Firestore, and read the gateway key"
    $projectInfo = Invoke-Gcloud -ArgumentList @("projects", "describe", $ProjectId, "--format=json")
    $projectNumber = ""
    if ($projectInfo.Output) {
        $projectNumber = [string]($projectInfo.Output | ConvertFrom-Json).projectNumber
    }
    if ([string]::IsNullOrWhiteSpace($projectNumber)) {
        throw "Could not read the project number for $ProjectId."
    }
    $projectNumber = $projectNumber.Trim()
    $RuntimeSa = "$projectNumber-compute@developer.gserviceaccount.com"
    Write-Host "Service account: $RuntimeSa"
    Write-Host "Granting Cloud Run Builder so Cloud Build can deploy. The first grant can take a couple of minutes to spread."
    $null = Invoke-Gcloud -ArgumentList @(
        "projects", "add-iam-policy-binding", $ProjectId,
        "--member", "serviceAccount:$RuntimeSa",
        "--role", "roles/run.builder",
        "--quiet"
    )
    $null = Invoke-Gcloud -ArgumentList @(
        "secrets", "add-iam-policy-binding", $AuthSecretName,
        "--member", "serviceAccount:$RuntimeSa",
        "--role", "roles/secretmanager.secretAccessor",
        "--quiet"
    )
    Write-Host "Granting Vertex AI User so the service account can call Gemini."
    $null = Invoke-Gcloud -ArgumentList @(
        "projects", "add-iam-policy-binding", $ProjectId,
        "--member", "serviceAccount:$RuntimeSa",
        "--role", "roles/aiplatform.user",
        "--quiet"
    )
    Write-Host "Granting Cloud Datastore User so the service account can read and write the inbox."
    $null = Invoke-Gcloud -ArgumentList @(
        "projects", "add-iam-policy-binding", $ProjectId,
        "--member", "serviceAccount:$RuntimeSa",
        "--role", "roles/datastore.user",
        "--quiet"
    )
    Ensure-FirestoreDatabase

    Write-Host ""
    Write-Host "Step 6 of 6: deploy bayos-gateway from this folder"
    Write-Host "The first build often takes several minutes."
    $setSecrets = "BAYOS_AUTH_KEY=${AuthSecretName}:latest"
    $setEnv = "GOOGLE_CLOUD_PROJECT=${ProjectId},GOOGLE_CLOUD_LOCATION=${VertexLocation},BAYOS_GEMINI_BACKEND=vertex"
    $null = Invoke-Gcloud -ArgumentList @(
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
        "--set-env-vars", $setEnv,
        "--quiet"
    )

    $serviceInfo = Invoke-Gcloud -ArgumentList @(
        "run", "services", "describe", $ServiceName, "--region", $Region, "--format=json"
    )
    $serviceUrl = ""
    if ($serviceInfo.Output) {
        $serviceUrl = [string]($serviceInfo.Output | ConvertFrom-Json).status.url
    }
    if ([string]::IsNullOrWhiteSpace($serviceUrl)) {
        throw "The deploy command finished, but the service URL could not be read."
    }
    $serviceUrl = $serviceUrl.Trim().TrimEnd("/")

    Write-Host ""
    Write-Host "Smoke test: /health"
    $headers = @{ "X-BayOS-Key" = $AuthKey }
    $health = $null
    $attempt = 0
    while ($attempt -lt 6) {
        $attempt++
        try {
            $health = Invoke-GatewayRequest -Method "Get" -Uri ($serviceUrl + "/health")
        } catch {
            if ($attempt -ge 6) {
                throw
            }
            Write-Host "The service is still starting. Waiting 10 seconds..."
            Start-Sleep -Seconds 10
            continue
        }
        $backendReady = ($health.gemini_backend -eq "vertex" -and $health.gemini_location -eq "global" -and $health.inbox_storage -eq "firestore")
        if ($backendReady) {
            break
        }
        if ($attempt -ge 6) {
            break
        }
        Write-Host "Waiting for Vertex and Firestore permissions to spread..."
        Start-Sleep -Seconds 20
    }
    Write-Host ("Health: " + ($health.status) + ", version " + ($health.version) + ", gemini " + ($health.gemini_backend) + " " + ($health.gemini_location) + " " + ($health.gemini_model) + ", inbox " + ($health.inbox_storage))
    if ($health.gemini_backend -ne "vertex" -or $health.gemini_location -ne "global") {
        throw "Health reported Gemini backend '$($health.gemini_backend)' at '$($health.gemini_location)'. Expected vertex at global. Pull the latest copy and run this script again."
    }
    if ($health.inbox_storage -ne "firestore") {
        throw "Health reported inbox_storage '$($health.inbox_storage)'. Expected firestore. Wait two minutes and run .\deploy.ps1 again so roles/datastore.user can spread."
    }

    Write-Host "Smoke test: store a note without calling Gemini, then mark it read"
    $noteTitle = "Deploy smoke note"
    $postJson = @{ agent = "tally-001"; title = $noteTitle; body = "Inbox stored without Gemini."; priority = "normal" } | ConvertTo-Json -Compress
    $posted = Invoke-GatewayRequest -Method "Post" -Uri ($serviceUrl + "/api/v1/inbox") -Headers $headers -JsonBody $postJson
    if ([string]::IsNullOrWhiteSpace([string]$posted.id)) {
        throw "The inbox post did not return an id."
    }
    $unread = Invoke-GatewayRequest -Method "Get" -Uri ($serviceUrl + "/api/v1/inbox?unread=true&limit=20") -Headers $headers
    if ($unread.storage -ne "firestore") {
        throw "Inbox list reported storage '$($unread.storage)'. Expected firestore."
    }
    $sawNote = $false
    foreach ($item in @($unread.items)) {
        if ($null -ne $item -and [string]$item.id -eq [string]$posted.id) {
            $sawNote = $true
        }
    }
    if (-not $sawNote) {
        throw "The posted note was not in the unread inbox."
    }
    $readItem = Invoke-GatewayRequest -Method "Post" -Uri ($serviceUrl + "/api/v1/inbox/" + $posted.id + "/read") -Headers $headers
    if ($readItem.unread) {
        throw "Marking the note read left it unread."
    }
    Write-Host ("Stored note " + $posted.id + " and marked it read.")

    Write-Host "Smoke test: authenticated ingest, then inbox"
    $marker = "Reply with the single word pong."
    $json = @{ task = $marker; agent = "prof-x-001" } | ConvertTo-Json -Compress
    $ingest = $null
    $ingestAttempt = 0
    while ($ingestAttempt -lt 4) {
        $ingestAttempt++
        try {
            $ingest = Invoke-GatewayRequest -Method "Post" -Uri ($serviceUrl + "/api/v1/task/ingest") -Headers $headers -JsonBody $json
            break
        } catch {
            if ($ingestAttempt -ge 4) {
                throw
            }
            Write-Host "Vertex permission can take a minute to spread. Waiting 20 seconds..."
            Start-Sleep -Seconds 20
        }
    }
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
    Write-Host '  .\bayos.ps1 post --title "Four walls" --body "Food, shelter, utilities, and auto are funded." --from tally-001'
    Write-Host "  .\bayos.ps1 read <id>"
    Write-Host ""
    Write-Host "The gateway key was not printed. Keep it in your password manager."
} finally {
    $AuthKey = $null
}

}
