#Requires -Version 5.1
<#
.SYNOPSIS
  Talk to a deployed BayOS gateway.

.DESCRIPTION
  bayos inbox
  bayos inbox --all
  bayos read <id>
  bayos post --title "title" --body "body" --from tally-001 --priority high --tag digest
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
    Write-Host "  bayos inbox --all"
    Write-Host "  bayos read <id>"
    Write-Host '  bayos post --title "title" --body "body" --from tally-001 --priority high --tag digest'
    Write-Host '  bayos send "text" --from prof-x-001'
    Write-Host ""
    Write-Host "inbox shows unread items. inbox --all shows every recent item."
    Write-Host "post stores a note and does not call Gemini. read marks one item read."
    Write-Host ""
    Write-Host "Set these first:"
    Write-Host '  $env:BAYOS_URL = "https://bayos-gateway-xxxxx-uc.a.run.app"'
    Write-Host '  $env:BAYOS_AUTH_KEY = "<your BayOS gateway key>"'
}

function ConvertTo-JsonString {
    param([AllowNull()][string]$Value)
    if ($null -eq $Value) {
        return "null"
    }
    $escaped = $Value.Replace("\", "\\").Replace('"', '\"').Replace("`r", "\r").Replace("`n", "\n").Replace("`t", "\t")
    return '"' + $escaped + '"'
}

function ConvertTo-BayosJson {
    # Windows PowerShell 5.1 ConvertTo-Json turns a one-item array into a string.
    param([Parameter(Mandatory = $true)]$Object)
    $parts = New-Object System.Collections.Generic.List[string]
    foreach ($key in @($Object.Keys)) {
        $value = $Object[$key]
        $isList = $false
        if ($null -ne $value -and -not ($value -is [string])) {
            if ($value -is [System.Array] -or $value -is [System.Collections.IList]) {
                $isList = $true
            }
        }
        if ($isList) {
            $items = New-Object System.Collections.Generic.List[string]
            foreach ($entry in @($value)) {
                [void]$items.Add((ConvertTo-JsonString -Value ([string]$entry)))
            }
            $encoded = "[" + ($items.ToArray() -join ",") + "]"
        } else {
            $encoded = ConvertTo-JsonString -Value ([string]$value)
        }
        [void]$parts.Add(((ConvertTo-JsonString -Value ([string]$key)) + ":" + $encoded))
    }
    return "{" + ($parts.ToArray() -join ",") + "}"
}

function Show-InboxItem {
    param($Item)
    if ($null -eq $Item) {
        return
    }
    $flag = "read"
    if ($Item.unread) {
        $flag = "unread"
    }
    $title = [string]$Item.title
    if ([string]::IsNullOrWhiteSpace($title)) {
        $title = [string]$Item.task
    }
    $body = [string]$Item.body
    if ([string]::IsNullOrWhiteSpace($body)) {
        $body = [string]$Item.output
    }
    Write-Host ("{0}  {1}  {2}  {3}  {4}" -f $Item.id, (Format-When $Item.received_at), $Item.agent, $Item.priority, $flag)
    Write-Host ("  {0}" -f $title)
    Write-Host ("  {0}" -f $body)
    $tagText = ""
    if ($null -ne $Item.tags) {
        $tagText = (@($Item.tags) | ForEach-Object { [string]$_ }) -join ", "
    }
    if (-not [string]::IsNullOrWhiteSpace($tagText)) {
        Write-Host ("  tags: {0}" -f $tagText)
    }
    Write-Host ""
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

# Dot-sourcing loads the functions for tests. `powershell -File .\bayos.ps1` still runs the client.
if ($MyInvocation.InvocationName -ne '.') {

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
    $showAll = $false
    foreach ($token in @($tokens)) {
        if ([string]$token -eq "--all") {
            $showAll = $true
        } else {
            Write-Host "inbox only accepts --all."
            Show-Usage
            exit 1
        }
    }
    $uri = $base + "/api/v1/inbox?limit=50"
    if (-not $showAll) {
        $uri += "&unread=true"
    }
    $inbox = Invoke-GatewayRequest -Method "Get" -Uri $uri -Headers $headers
    Write-Host $inbox.notice
    Write-Host ""
    if ($inbox.count -eq 0) {
        if ($showAll) {
            Write-Host "Inbox is empty."
        } else {
            Write-Host "No unread items."
        }
        if ($inbox.storage -ne "firestore") {
            Write-Host "This list is in memory on one instance. It is cleared when Cloud Run scales to zero."
        }
        exit 0
    }
    foreach ($item in @($inbox.items)) {
        Show-InboxItem -Item $item
    }
    exit 0
}

if ($command -eq "read") {
    if ($tokens.Count -ne 1 -or [string]::IsNullOrWhiteSpace([string]$tokens[0])) {
        Write-Host "read needs the inbox id."
        Show-Usage
        exit 1
    }
    $itemId = ([string]$tokens[0]).Trim()
    $item = Invoke-GatewayRequest -Method "Post" -Uri ($base + "/api/v1/inbox/" + $itemId + "/read") -Headers $headers
    Write-Host "Marked read."
    Show-InboxItem -Item $item
    exit 0
}

if ($command -eq "post") {
    $fromAgent = "prof-x-001"
    $title = ""
    $body = ""
    $priority = "normal"
    $tags = New-Object System.Collections.Generic.List[string]
    $positionals = New-Object System.Collections.Generic.List[string]
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
        } elseif ($token -eq "--title") {
            $i++
            if ($i -ge $tokens.Count) {
                Write-Host "Missing the text after --title."
                Show-Usage
                exit 1
            }
            $title = [string]$tokens[$i]
        } elseif ($token -eq "--body") {
            $i++
            if ($i -ge $tokens.Count) {
                Write-Host "Missing the text after --body."
                Show-Usage
                exit 1
            }
            $body = [string]$tokens[$i]
        } elseif ($token -eq "--priority") {
            $i++
            if ($i -ge $tokens.Count) {
                Write-Host "Missing the value after --priority."
                Show-Usage
                exit 1
            }
            $priority = [string]$tokens[$i]
        } elseif ($token -eq "--tag") {
            $i++
            if ($i -ge $tokens.Count) {
                Write-Host "Missing the value after --tag."
                Show-Usage
                exit 1
            }
            [void]$tags.Add([string]$tokens[$i])
        } else {
            [void]$positionals.Add($token)
        }
    }
    if ([string]::IsNullOrWhiteSpace($title) -and $positionals.Count -ge 1) {
        $title = [string]$positionals[0]
    }
    if ([string]::IsNullOrWhiteSpace($body) -and $positionals.Count -ge 2) {
        $body = [string]$positionals[1]
    }
    $title = $title.Trim()
    $body = $body.Trim()
    if ([string]::IsNullOrWhiteSpace($title) -or [string]::IsNullOrWhiteSpace($body)) {
        Write-Host "post needs a title and a body."
        Show-Usage
        exit 1
    }
    $payload = @{
        agent    = $fromAgent
        title    = $title
        body     = $body
        priority = $priority
        tags     = $tags.ToArray()
    }
    $json = ConvertTo-BayosJson -Object $payload
    $result = Invoke-GatewayRequest -Method "Post" -Uri ($base + "/api/v1/inbox") -Headers $headers -JsonBody $json
    Write-Host ("stored: " + $result.id)
    Write-Host ("from: " + $result.agent)
    Write-Host ("title: " + $result.title)
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

}
