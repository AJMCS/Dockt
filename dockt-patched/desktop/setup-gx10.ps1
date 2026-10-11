# One-time setup so Dockt can reach the GX10's models through an SSH tunnel (no password prompts later).
# Run it with desktop\Setup-GX10.bat. It:
#   1. asks for the GX10 login (e.g. asus@169.233.72.21) and saves it to gx10.txt,
#   2. creates an SSH key for this laptop if there isn't one,
#   3. adds the key to the GX10 (asks for the GX10 password once),
#   4. checks that a password-less login and the Ollama port both work.
$root = Split-Path -Parent $PSScriptRoot
$gx10File = Join-Path $root "gx10.txt"

$current = ""
if (Test-Path $gx10File) { $current = ((Get-Content $gx10File -Raw) -split "\s+" | Where-Object { $_ })[0] }
$prompt = "GX10 login, user@address (e.g. asus@169.233.72.21)"
if ($current) { $prompt += " [$current]" }
$target = (Read-Host $prompt).Trim()
if (-not $target) { $target = $current }
if ($target -notmatch "^[^@\s]+@[^@\s]+$") { Write-Host "Expected user@address, got '$target'."; exit 1 }

if (-not (Get-Command ssh -ErrorAction SilentlyContinue)) {
  Write-Host "The OpenSSH client isn't installed. Settings > System > Optional features > add 'OpenSSH Client', then run this again."
  exit 1
}

$sshDir = Join-Path $HOME ".ssh"
$key = Join-Path $sshDir "id_ed25519"
if (-not (Test-Path $key)) {
  New-Item -ItemType Directory -Force -Path $sshDir | Out-Null
  # one raw argument string so the empty passphrase survives both Windows PowerShell 5 and PowerShell 7
  Start-Process ssh-keygen -ArgumentList "-t ed25519 -N `"`" -q -C `"dockt-$env:COMPUTERNAME`" -f `"$key`"" -NoNewWindow -Wait
}
if (-not (Test-Path "$key.pub")) { Write-Host "Couldn't create an SSH key at $key."; exit 1 }

$opts = @("-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10")
& ssh @opts -o BatchMode=yes $target "exit" 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
  Write-Host "Adding this laptop's key to $target. Enter the GX10 password when asked."
  $pub = (Get-Content "$key.pub" -Raw).Trim()
  # the leading newline keeps the key on its own line even if authorized_keys doesn't end with one
  & ssh @opts $target "mkdir -p ~/.ssh && chmod 700 ~/.ssh && printf '\n%s\n' '$pub' >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
  if ($LASTEXITCODE -ne 0) { Write-Host "Couldn't add the key (wrong password, or the GX10 isn't reachable from this network)."; exit 1 }
  & ssh @opts -o BatchMode=yes $target "exit"
  if ($LASTEXITCODE -ne 0) { Write-Host "The key was added but a password-less login still fails."; exit 1 }
}

$check = & ssh @opts -o BatchMode=yes $target "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:11434/v1/models"
if ($check -ne "200") { Write-Host "Logged in, but Ollama didn't answer on the GX10 (got '$check'). Ask whoever runs the GX10."; exit 1 }

Set-Content -Path $gx10File -Value $target -Encoding ascii
Write-Host ""
Write-Host "Done. gx10.txt now says $target, and Dockt will open the tunnel itself every time it starts."
