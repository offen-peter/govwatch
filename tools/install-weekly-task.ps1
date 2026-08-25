<#
    Registers the weekly local transcribe as a Windows scheduled task.

    Run this once, as your normal user. Do not run it elevated: the task
    has to run as you so it inherits your ANTHROPIC_API_KEY and your git
    credentials, and an elevated registration would attach it to the
    administrator account instead.

    Re-running is safe. It replaces the existing task rather than adding
    a second one.
#>

$ErrorActionPreference = "Stop"

$TaskName = "GovWatch weekly transcribe"
$Script   = "C:\Users\pboff\OneDrive\Documents\Claude Code\govwatch\tools\transcribe-weekly.ps1"

if (-not (Test-Path $Script)) {
    throw "Cannot find $Script"
}

$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$Script`""

# Wednesday morning. The three bodies meet on Monday evenings, the video
# window opens the next day, and captions are usually up overnight, so
# Wednesday catches Monday's meeting with a day to spare. The exact day
# matters less than it looks: video.lookback_days is 45, so a run that
# slips still sweeps up everything from previous weeks.
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Wednesday -At 9am

# StartWhenAvailable is the setting that makes this trustworthy on a
# laptop. Without it a machine that is asleep at 9am on Wednesday simply
# skips the week in silence, which is the failure mode this whole
# arrangement exists to avoid. With it, the task runs when the machine
# next comes back.
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Fetches city and school board meeting captions from a residential connection, because YouTube refuses them from the GitHub runner. Commits and pushes the transcripts." `
    -Force | Out-Null

Write-Output "Registered: $TaskName"
Write-Output ""
Write-Output "Next run:"
(Get-ScheduledTaskInfo -TaskName $TaskName).NextRunTime
Write-Output ""
Write-Output "Run it now to check it works:"
Write-Output "  Start-ScheduledTask -TaskName '$TaskName'"
Write-Output "Logs land in state\logs\."
