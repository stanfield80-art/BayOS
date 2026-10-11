#Requires -Version 5.1
<#
.SYNOPSIS
  Talk to a deployed BayOS gateway.

.DESCRIPTION
  bayos inbox
  bayos send "text" --from prof-x-001

  Reads the gateway address from BAYOS_URL and the key from BAYOS_AUTH_KEY.
  Sends the key on the X-BayOS-Key header.
#>

$ErrorActionPreference = "Stop"
if (Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
    $PSNativeCommandUseErrorActionPreference = $false
}

function Format-When {
    param($Value)
    if ($Value -is [datetime]) {
        return $Value.ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    }
    return [string]$Value
}

function Show-Usage {
    Write-Host "Usage:"
    Write-Host "  bayos inbox"
    Write-Host '  bayos send "text" --from prof-x-001'
    Write-Host ""
    Write-Host "Set these first:"
    Write-Host '  $env:BAYOS_URL = "https://bayos-gateway-xxxxx-uc.a.run.app"'
    Write-Host '  $env:BAYOS_AUTH_KEY = "<your BayOS gateway key>"'
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
        Write-Host "Call to $Uri failed."
        Write-Host $detail
        exit 1
    }
}

if (-not $args -or $args.Count -lt 1) {
    Show-Usage
    exit 1
}

$command = ("" + $args[0]).ToLower()
$tokens = @()
if ($args.Count -gt 1) {
    $tokens = @($args[1..($args.Count - 1)])
}

if ([string]::IsNullOrWhiteSpace($env:BAYOS_URL)) {
    Write-Host "BAYOS_URL is not set."
    Show-Usage
    exit 1
}
if ([string]::IsNullOrWhiteSpace($env:BAYOS_AUTH_KEY)) {
    Write-Host "BAYOS_AUTH_KEY is not set."
    Show-Usage
    exit 1
}

$base = $env:BAYOS_URL.Trim().TrimEnd("/")
$headers = @{ "X-BayOS-Key" = $env:BAYOS_AUTH_KEY }

if ($command -eq "inbox") {
    if ($tokens.Count -gt 0) {
        Write-Host "inbox does not take extra words."
        Show-Usage
        exit 1
    }
    $inbox = Invoke-GatewayRequest -Method "Get" -Uri ($base + "/api/v1/inbox") -Headers $headers
    Write-Host $inbox.notice
    Write-Host ""
    if ($inbox.count -eq 0) {
        Write-Host "Inbox is empty."
        Write-Host "If the service has been idle, Cloud Run may have shut the instance down and cleared this list."
        exit 0
    }
    foreach ($item in @($inbox.items)) {
        if ($null -eq $item) {
            continue
        }
        Write-Host ("{0}  from {1}  {2}" -f (Format-When $item.received_at), $item.agent, $item.status)
        Write-Host ("  task: {0}" -f $item.task)
        Write-Host ("  output: {0}" -f $item.output)
        Write-Host ""
    }
    exit 0
}

if ($command -eq "send") {
    $fromAgent = "prof-x-001"
    $textParts = New-Object System.Collections.Generic.List[string]
    for ($i = 0; $i -lt $tokens.Count; $i++) {
        $token = [string]$tokens[$i]
        if ($token -eq "--from" -or $token -eq "-from" -or $token -eq "-From") {
            $i++
            if ($i -ge $tokens.Count) {
                Write-Host "Missing the agent id after --from."
                Show-Usage
                exit 1
            }
            $fromAgent = [string]$tokens[$i]
        } else {
            [void]$textParts.Add($token)
        }
    }
    $text = ($textParts.ToArray() -join " ").Trim()
    if ([string]::IsNullOrWhiteSpace($text)) {
        Write-Host "Missing the message text."
        Show-Usage
        exit 1
    }
    $json = @{ task = $text; agent = $fromAgent } | ConvertTo-Json -Compress
    $result = Invoke-GatewayRequest -Method "Post" -Uri ($base + "/api/v1/task/ingest") -Headers $headers -JsonBody $json
    Write-Host ("status: " + $result.status)
    Write-Host ("from: " + $result.agent)
    Write-Host ("output: " + $result.output)
    exit 0
}

Write-Host "Unknown command: $command"
Show-Usage
exit 1
