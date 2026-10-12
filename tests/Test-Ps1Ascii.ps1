#Requires -Version 5.1
# Fails when any .ps1 contains a byte above 0x7F.
# Windows PowerShell 5.1 reads a UTF-8 file with no BOM as ANSI, so those
# bytes break the parser. Non-ASCII output belongs in [char] codes.

$ErrorActionPreference = "Stop"
$failures = New-Object System.Collections.Generic.List[string]

function Check {
    param([string]$Name, [bool]$Ok, [string]$Detail = "")
    if ($Ok) {
        Write-Host ("PASS " + $Name)
    } else {
        Write-Host ("FAIL " + $Name + " " + $Detail)
        [void]$script:failures.Add($Name)
    }
}

$root = Split-Path -Parent $PSScriptRoot
$scripts = @(Get-ChildItem -Path $root -Filter *.ps1 -Recurse -File)
Check -Name "found scripts" -Ok ($scripts.Count -ge 3) -Detail ([string]$scripts.Count)

foreach ($script in $scripts) {
    $bytes = [System.IO.File]::ReadAllBytes($script.FullName)
    $bad = -1
    for ($i = 0; $i -lt $bytes.Length; $i++) {
        if ($bytes[$i] -gt 127) {
            $bad = $i
            break
        }
    }
    $relative = $script.FullName.Substring($root.Length).TrimStart('\', '/')
    $detail = ""
    if ($bad -ge 0) {
        $detail = $relative + " byte " + $bad + " is 0x" + $bytes[$bad].ToString("X2")
    }
    Check -Name ("ascii " + $relative) -Ok ($bad -lt 0) -Detail $detail
}

$vault = Join-Path ([System.IO.Path]::GetTempPath()) ("bayos-ascii-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $vault -Force | Out-Null
$env:BAYOS_VAULT = $vault
Remove-Item Env:BAYOS_AUTH_KEY -ErrorAction SilentlyContinue
Remove-Item Env:BAYOS_URL -ErrorAction SilentlyContinue

. (Join-Path $root "bayos.ps1")
$save = Get-Command -Name Save-BayosAuthKey -ErrorAction SilentlyContinue
Check -Name "Save-BayosAuthKey after dot-source" -Ok ($null -ne $save) -Detail "command missing"

$emptyMessage = ""
try {
    Save-BayosAuthKey -Plain ([string][char]1 + [string][char]127 + "`r`n") | Out-Null
} catch {
    $emptyMessage = $_.Exception.Message
}
Check -Name "control-only key is empty" -Ok ($emptyMessage -eq "The gateway key was empty.") -Detail $emptyMessage

$windowsMessage = ""
try {
    Save-BayosAuthKey -Plain ("ab" + [char]1 + "cd") | Out-Null
} catch {
    $windowsMessage = $_.Exception.Message
}
Check -Name "dotted save reaches DPAPI" -Ok ($windowsMessage -match "Windows") -Detail $windowsMessage

. (Join-Path $root "deploy.ps1")
$dirty = "ab" + [char]1 + "cd" + [char]127 + "`r`n" + [char]0xFEFF + [char]0x200B + "ef  "
$cleaned = Remove-ControlCharacters -Value $dirty
Check -Name "deploy strips controls" -Ok ($cleaned -eq "abcdef") -Detail $cleaned
Check -Name "deploy keeps inner space" -Ok ((Remove-ControlCharacters -Value "ab cd") -eq "ab cd")
$reader = (Get-Command Read-HiddenValue).Definition
Check -Name "hidden prompt strips" -Ok ($reader.Contains("Remove-ControlCharacters")) -Detail "Read-HiddenValue does not call Remove-ControlCharacters"

$blankCalendar = Test-BayosCalendarLink -Value "   "
Check -Name "blank calendar skipped" -Ok ($blankCalendar -eq $false) -Detail ([string]$blankCalendar)
$httpsCalendar = Test-BayosCalendarLink -Value "https://example.com/cal"
Check -Name "https calendar accepted" -Ok ($httpsCalendar -eq $true)
$webcalCalendar = Test-BayosCalendarLink -Value "webcal://example.com/cal"
Check -Name "webcal calendar accepted" -Ok ($webcalCalendar -eq $true)
$httpMessage = ""
try {
    Test-BayosCalendarLink -Value "http://example.com/cal" | Out-Null
} catch {
    $httpMessage = $_.Exception.Message
}
Check -Name "http calendar rejected" -Ok ($httpMessage -match "https://") -Detail $httpMessage

Write-Host ("FAILURES " + $failures.Count)
if ($failures.Count -gt 0) {
    exit 1
}
exit 0
