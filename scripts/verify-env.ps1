# Point this terminal at the verification sandbox.
#
#   . .\scripts\verify-env.ps1          # dot-source it, in EVERY terminal
#   . .\scripts\verify-env.ps1 -Fresh   # and start from an empty database
#
# ## Why this exists
#
# The server, the seeding script and the desktop each read their database and
# storage locations from the environment. If one terminal has them and another
# does not, the two halves quietly use *different databases* — and the symptom
# is `{"error":"no such project"}` against a project you watched get created a
# moment earlier. Nothing is broken; they are simply looking in different
# places, and there is no message that says so.
#
# Six exports typed by hand into three terminals is six chances to get that
# wrong, and closing the editor loses all of them at once.
#
# ## Why it must be dot-sourced
#
# `.\scripts\verify-env.ps1` runs in a child process and its variables die with
# it. `. .\scripts\verify-env.ps1` — note the leading dot and space — runs it in
# *this* shell, which is the only way an environment variable survives. The
# check at the end catches the mistake rather than leaving it to be discovered
# three commands later.

param(
    # Delete the sandbox first. Everything here is throwaway by design: a
    # separate database and storage tree under `var\verify`, nothing shared with
    # a real project.
    [switch]$Fresh
)

$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$sandbox = Join-Path $root "var\verify"

if ($Fresh) {
    if (Test-Path $sandbox) {
        Remove-Item -Recurse -Force $sandbox
        Write-Host "removed the old sandbox at $sandbox" -ForegroundColor Yellow
    }
    # The device identity too, or the desktop keeps a token for a device the
    # new database has never heard of and every claim comes back 401.
    $identity = Join-Path $env:LOCALAPPDATA "VoiceToVideo"
    if (Test-Path $identity) {
        Remove-Item -Recurse -Force $identity
        Write-Host "unpaired this computer ($identity)" -ForegroundColor Yellow
    }
}

New-Item -ItemType Directory -Force -Path $sandbox | Out-Null

$env:VTV_ENV = "development"
$env:VTV_STORAGE_ROOT = Join-Path $sandbox "storage"
$env:VTV_DATABASE_URL = "sqlite:///" + ($sandbox -replace '\\', '/') + "/vtv.db"
$env:VTV_SIGNING_KEY = "verify-only-not-a-real-secret"
$env:VTV_DESKTOP_HOME = Join-Path $sandbox "desktop"
$env:PYTHONPATH = Join-Path $root "src"

Write-Host ""
Write-Host "verification sandbox ready" -ForegroundColor Green
Write-Host "  database : $($env:VTV_DATABASE_URL)"
Write-Host "  storage  : $($env:VTV_STORAGE_ROOT)"
Write-Host "  identity : $($env:VTV_DESKTOP_HOME)"
Write-Host ""
Write-Host "Run this in EVERY terminal you use, or they will read different databases." -ForegroundColor Cyan
Write-Host ""
