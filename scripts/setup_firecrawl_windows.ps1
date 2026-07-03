# Windows-side portproxy + firewall for Firecrawl on WSL2.
# Run in Administrator PowerShell on the VPS (or invoked from bootstrap_firecrawl_wsl.sh).
#
# Usage:
#   .\setup_firecrawl_windows.ps1 -WslIp 172.20.1.2 -AllowedClientIp 58.44.21.62

param(
    [Parameter(Mandatory = $true)]
    [string]$WslIp,

    [Parameter(Mandatory = $true)]
    [string]$AllowedClientIp,

    [int]$Port = 3002,

    [string]$ThinkbookSshPubKey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGyYA52oQLR0mhTkTc4Ug9x6s7a12Z1KG9VWQtG5n/MQ loong@thinkbook"
)

$ErrorActionPreference = "Stop"

$keyFile = "C:\ProgramData\ssh\administrators_authorized_keys"
if (Test-Path $keyFile) {
    $content = Get-Content $keyFile -Raw
    $fp = $ThinkbookSshPubKey.Split()[0]
    if ($content -notmatch [regex]::Escape($fp)) {
        Add-Content -Path $keyFile -Value $ThinkbookSshPubKey -Encoding Ascii
        icacls.exe $keyFile /inheritance:r /grant "SYSTEM:(F)" /grant "Administrators:(F)" | Out-Null
        Write-Host "Appended thinkbook SSH pubkey to administrators_authorized_keys"
    }
}

netsh interface portproxy delete v4tov4 listenport=$Port listenaddress=0.0.0.0 2>$null | Out-Null
netsh interface portproxy add v4tov4 listenport=$Port listenaddress=0.0.0.0 connectport=$Port connectaddress=$WslIp
netsh interface portproxy show v4tov4

$ruleName = "Firecrawl $Port"
Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Protocol TCP -LocalPort $Port `
    -RemoteAddress "$AllowedClientIp/32" -Action Allow -Enabled True | Out-Null
Write-Host "Firewall: $ruleName allows $AllowedClientIp -> TCP $Port"
