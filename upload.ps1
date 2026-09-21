# WB Switch GitHub upload script (version-gated).
# Rule: upload ONLY when the version number changes. Old version tags are never deleted.
# Usage:
#   First time:  .\upload.ps1 -Remote https://github.com/<your-name>/<repo>.git
#   Afterwards:  .\upload.ps1
param([string]$Remote = '')
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

# locate git
$git = $null
foreach ($cand in @('git',
    "$env:USERPROFILE\.workbuddy\binaries\PortableGit\versions\1.2.0\cmd\git.exe",
    'C:\Program Files\Git\cmd\git.exe')) {
  try { $null = Get-Command $cand -ErrorAction Stop; $git = $cand; break } catch { }
}
if (-not $git) { Write-Host '[X] git not found. Install Git or add it to PATH.'; exit 1 }

$ver = (Get-Content VERSION -Raw).Trim()
$tag = "v$ver"

if ($Remote -ne '') {
  & $git remote remove origin 2>$null
  & $git remote add origin $Remote
}
$remoteUrl = & $git remote get-url origin 2>$null
if (-not $remoteUrl) {
  Write-Host '[X] No remote configured. First time run: .\upload.ps1 -Remote https://github.com/<you>/<repo>.git'
  exit 1
}

Write-Host "Version: $tag   Remote: $remoteUrl"

# version gate: if this tag already exists on the remote, skip
$remoteTags = & $git ls-remote --tags origin 2>$null
if ($LASTEXITCODE -ne 0) { Write-Host '[X] Cannot reach remote repo. Check network / URL / login.'; exit 1 }
if ($remoteTags -match "refs/tags/$tag`$") {
  Write-Host "= $tag already exists on remote. Version unchanged, skip upload."
  exit 0
}

& $git add -A
$null = & $git commit -m "release $tag" 2>$null
& $git tag -f $tag
& $git push origin HEAD:main
& $git push origin $tag
if ($LASTEXITCODE -eq 0) { Write-Host "[OK] Uploaded $tag (all old version tags are kept)" } else { Write-Host '[X] push failed'; exit 1 }
