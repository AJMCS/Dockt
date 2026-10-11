# One-time setup so Dockt can reach the GX10's models through an SSH tunnel (no password prompts later).
# Run it with desktop\Setup-GX10.bat. It:
#   1. asks for the GX10 login (e.g. asus@169.233.72.21) and saves it to gx10.txt,
#   2. checks that the GX10's SSH port can be reached from this network,
#   3. creates an SSH key for this laptop if there isn't one,
#   4. adds the key to the GX10 (asks for the GX10 password once),
#   5. checks that a password-less login and the Ollama port both work.
# Every step prints what it is doing, and every step that needs no typing runs with no console input and a time
# limit: Windows' ssh.exe can otherwise sit waiting on the console after the remote command has finished.
# Keep this file ASCII: Windows PowerShell 5 reads a script without a BOM as ANSI.
$root = Split-Path -Parent $PSScriptRoot
$gx10File = Join-Path $root "gx10.txt"

$current = ""
if (Test-Path $gx10File) { $current = ((Get-Content $gx10File -Raw) -split "\s+" | Where-Object { $_ })[0] }
$prompt = "GX10 login, user@address (e.g. asus@169.233.72.21)"
if ($current) { $prompt += " [$current]" }
$target = (Read-Host $prompt).Trim()
if (-not $target) { $target = $current }
if ($target -notmatch "^[^@\s]+@[^@\s]+$") { Write-Host "Expected user@address, got '$target'."; exit 1 }
$gxHost = $target.Split("@")[1]

# Prefer Windows' own OpenSSH: another ssh.exe on PATH (e.g. Git's) can't show a password prompt in this window.
function Find-Tool([string]$name) {
  if ($env:WINDIR) {
    $builtin = Join-Path $env:WINDIR "System32\OpenSSH\$name.exe"
    if (Test-Path $builtin) { return $builtin }
  }
  $cmd = Get-Command $name -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }
  return $null
}
$ssh = Find-Tool "ssh"
$keygen = Find-Tool "ssh-keygen"
if (-not $ssh -or -not $keygen) {
  Write-Host "The OpenSSH client isn't installed. Settings > System > Optional features > add 'OpenSSH Client', then run this again."
  exit 1
}
Write-Host "Using $ssh"

# Run a command that needs no typing: no console input, output captured, killed after $seconds.
function Invoke-Quiet([string]$exe, [string]$arguments, [int]$seconds = 25) {
  $psi = New-Object System.Diagnostics.ProcessStartInfo
  $psi.FileName = $exe; $psi.Arguments = $arguments
  $psi.UseShellExecute = $false; $psi.CreateNoWindow = $true
  $psi.RedirectStandardInput = $true; $psi.RedirectStandardOutput = $true; $psi.RedirectStandardError = $true
  $p = [System.Diagnostics.Process]::Start($psi)
  $p.StandardInput.Close()
  $out = $p.StandardOutput.ReadToEndAsync(); $err = $p.StandardError.ReadToEndAsync()
  if (-not $p.WaitForExit($seconds * 1000)) {
    try { $p.Kill() } catch {}
    return @{ code = -1; out = ""; err = "no answer after $seconds seconds" }
  }
  $p.WaitForExit()
  return @{ code = $p.ExitCode; out = $out.Result.Trim(); err = $err.Result.Trim() }
}

function Test-Port([string]$hostName, [int]$port, [int]$ms = 5000) {
  $c = New-Object System.Net.Sockets.TcpClient
  try { $r = $c.BeginConnect($hostName, $port, $null, $null); return ($r.AsyncWaitHandle.WaitOne($ms) -and $c.Connected) }
  catch { return $false }
  finally { $c.Close() }
}

Write-Host "Checking that $gxHost answers on the SSH port..."
if (-not (Test-Port $gxHost 22)) {
  Write-Host "Can't reach $gxHost on port 22 from this network. Check the address, and that this laptop is on the same Wi-Fi as the GX10 (guest networks often block it)."
  exit 1
}

$sshDir = Join-Path $HOME ".ssh"
$key = Join-Path $sshDir "id_ed25519"
if (-not (Test-Path $key)) {
  Write-Host "Creating an SSH key for this laptop..."
  New-Item -ItemType Directory -Force -Path $sshDir | Out-Null
  # one raw argument string so the empty passphrase (-N "") survives both Windows PowerShell 5 and PowerShell 7
  $r = Invoke-Quiet $keygen "-t ed25519 -N `"`" -q -C `"dockt-$env:COMPUTERNAME`" -f `"$key`""
  if ($r.code -ne 0) { Write-Host "ssh-keygen failed: $($r.err)" }
}
if (-not (Test-Path "$key.pub")) { Write-Host "Couldn't create an SSH key at $key."; exit 1 }

$opts = "-o StrictHostKeyChecking=accept-new -o ConnectTimeout=10"
$batch = "$opts -o BatchMode=yes -i `"$key`" $target"

Write-Host "Trying a password-less login..."
$r = Invoke-Quiet $ssh "$batch exit"
if ($r.code -ne 0) {
  if ($r.err -match "Permission denied") {
    Write-Host ""
    Write-Host "Adding this laptop's key to $target. Type the GX10 password when asked (nothing shows while you type), then press Enter."
    $pub = (Get-Content "$key.pub" -Raw).Trim()
    # -n: the remote command needs no input, so ssh mustn't wait on this console after it finishes.
    # The leading newline keeps the key on its own line even if authorized_keys doesn't end with one.
    & $ssh -n -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 $target "mkdir -p ~/.ssh && chmod 700 ~/.ssh && printf '\n%s\n' '$pub' >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
    if ($LASTEXITCODE -ne 0) { Write-Host "Couldn't add the key (wrong password?)."; exit 1 }
    $r = Invoke-Quiet $ssh "$batch exit"
    if ($r.code -ne 0) { Write-Host "The key was added but a password-less login still fails: $($r.err)"; exit 1 }
  } else {
    Write-Host "Couldn't log in to ${target}: $($r.err)"
    exit 1
  }
}

Write-Host "Checking Ollama on the GX10..."
$r = Invoke-Quiet $ssh "$batch `"curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:11434/v1/models`""
if ($r.out -ne "200") { Write-Host "Logged in, but Ollama didn't answer on the GX10 (got '$($r.out)' $($r.err)). Ask whoever runs the GX10."; exit 1 }

Set-Content -Path $gx10File -Value $target -Encoding ascii
Write-Host ""
Write-Host "Done. gx10.txt now says $target, and Dockt will open the tunnel itself every time it starts."
