# One-shot: add thinkbook agent SSH key on Windows OpenSSH (Administrator PowerShell).
$pubKey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGyYA52oQLR0mhTkTc4Ug9x6s7a12Z1KG9VWQtG5n/MQ loong@thinkbook"
$keyFile = "C:\ProgramData\ssh\administrators_authorized_keys"
if (-not (Test-Path $keyFile)) { New-Item -Path $keyFile -ItemType File -Force | Out-Null }
$content = Get-Content $keyFile -Raw -ErrorAction SilentlyContinue
if ($content -notmatch [regex]::Escape($pubKey.Split()[0])) {
    Add-Content -Path $keyFile -Value $pubKey -Encoding Ascii
}
icacls.exe $keyFile /inheritance:r /grant "SYSTEM:(F)" /grant "Administrators:(F)"
Write-Host "Key ready at $keyFile"
