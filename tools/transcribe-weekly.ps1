<#
    Weekly local transcribe, the cloud half of which does not work.

    YouTube challenges datacenter addresses and refuses caption requests
    from GitHub Actions. Measured repeatedly, most recently on
    2026-08-24, with the proof of origin token provider confirmed up and
    answering: "Sign in to confirm you're not a bot". The same fetches
    succeed from a residential connection in seconds.

    So the split this repo already documented is now the arrangement.
    Actions does documents, the county's Vimeo video, and the digest,
    none of which touch yt-dlp. This script does city and school board
    video from a machine YouTube will actually talk to, and pushes the
    transcripts back so the next digest can read them.

    It is deliberately a plain scheduled command rather than anything
    cleverer. The pipeline already decides what is worth fetching, and
    video.lookback_days is 45, so a weekly run has a wide margin: a
    missed week still catches everything the following week.

    Register it with tools/install-weekly-task.ps1.
#>

$ErrorActionPreference = "Stop"

$Repo   = "C:\Users\pboff\OneDrive\Documents\Claude Code\govwatch"
$Python = "C:\Users\pboff\AppData\Local\Programs\Python\Python311\python.exe"
$LogDir = Join-Path $Repo "state\logs"
$Log    = Join-Path $LogDir ("transcribe-" + (Get-Date -Format "yyyy-MM-dd-HHmm") + ".log")

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Say($msg) {
    $line = "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Write-Output $line
    Add-Content -Path $Log -Value $line -Encoding utf8
}

Say "starting weekly transcribe"

# The key is read from the environment and never stored here. Set it once
# for your user account with:
#   setx ANTHROPIC_API_KEY "sk-ant-..."
# then sign out and back in, or reboot, so the scheduler inherits it.
if (-not $env:ANTHROPIC_API_KEY) {
    Say "ANTHROPIC_API_KEY is not set for this account, stopping before doing any work."
    Say "Set it with: setx ANTHROPIC_API_KEY ""sk-ant-..."" then sign out and back in."
    exit 1
}

Set-Location $Repo

# Start from whatever Actions has committed since the last local run,
# otherwise the push at the end lands on a stale base and is rejected.
Say "pulling"
git pull --rebase --quiet
if ($LASTEXITCODE -ne 0) {
    Say "git pull failed, exit $LASTEXITCODE. Not transcribing onto an unknown base."
    exit 1
}

Say "running transcribe"
& $Python "run.py" "transcribe" 2>&1 | ForEach-Object { Say $_ }
$transcribeExit = $LASTEXITCODE

# Commit whatever landed even if the run reported a failure. A refused
# city video and a successful school board one happen in the same run,
# and the transcript that did arrive is worth keeping either way.
$dirty = git status --porcelain -- transcripts state
if ($dirty) {
    Say "committing new transcripts and state"
    git add transcripts state
    git commit --quiet -m "govwatch: local transcribe $(Get-Date -Format yyyy-MM-dd)

Run from a residential connection, because YouTube refuses caption
requests from the GitHub runner. See tools/transcribe-weekly.ps1."
    git push --quiet
    if ($LASTEXITCODE -ne 0) {
        Say "git push failed, exit $LASTEXITCODE. The work is committed locally, push by hand."
        exit 1
    }
    Say "pushed"
} else {
    Say "nothing new, no commit"
}

if ($transcribeExit -ne 0) {
    Say "transcribe exited $transcribeExit, see above"
    exit $transcribeExit
}

Say "done"
