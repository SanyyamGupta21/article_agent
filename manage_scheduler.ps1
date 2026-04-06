# manage_scheduler.ps1
# Quick management helper for digest scheduled tasks.
# Examples:
#   .\manage_scheduler.ps1 -Action status -Persona all
#   .\manage_scheduler.ps1 -Action off -Persona ai
#   .\manage_scheduler.ps1 -Action on -Persona law
#   .\manage_scheduler.ps1 -Action toggle -Persona all
#   .\manage_scheduler.ps1 -Action run -Persona ai

param(
    [ValidateSet("status", "on", "off", "toggle", "run", "monitor")]
    [string]$Action = "status",

    [ValidateSet("ai", "law", "all")]
    [string]$Persona = "all"
)

$runHistoryPath = Join-Path $PSScriptRoot "logs\digest_runs.jsonl"

$taskMap = @{
    ai  = "AIDigestBot"
    law = "LawDigestBot"
}

function Resolve-Targets {
    param([string]$PersonaValue)

    if ($PersonaValue -eq "all") {
        return @($taskMap.Keys)
    }
    return @($PersonaValue)
}

function Get-TaskOrNull {
    param([string]$TaskName)
    return Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
}

function Parse-IsoDurationHours {
    param([string]$Duration)

    if (-not $Duration) {
        return $null
    }

    if ($Duration -match '^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$') {
        $h = if ($Matches[1]) { [double]$Matches[1] } else { 0 }
        $m = if ($Matches[2]) { [double]$Matches[2] } else { 0 }
        $s = if ($Matches[3]) { [double]$Matches[3] } else { 0 }
        return $h + ($m / 60.0) + ($s / 3600.0)
    }

    return $null
}

function Get-LastRunRecord {
    param([string]$PersonaValue)

    if (-not (Test-Path $runHistoryPath)) {
        return $null
    }

    $records = Get-Content $runHistoryPath -ErrorAction SilentlyContinue |
        Where-Object { -not [string]::IsNullOrWhiteSpace($_) } |
        ForEach-Object {
            try {
                $_ | ConvertFrom-Json
            }
            catch {
                $null
            }
        } |
        Where-Object { $_ -and $_.persona -eq $PersonaValue }

    if ($null -eq $records) {
        return $null
    }

    return $records | Sort-Object timestamp_utc -Descending | Select-Object -First 1
}

$targets = Resolve-Targets -PersonaValue $Persona

foreach ($key in $targets) {
    $taskName = $taskMap[$key]
    $task = Get-TaskOrNull -TaskName $taskName

    if ($null -eq $task) {
        Write-Host ("[{0}] Task not found: {1}. Register it first with setup_scheduler.ps1." -f $key.ToUpper(), $taskName)
        continue
    }

    switch ($Action) {
        "status" {
            $info = Get-ScheduledTaskInfo -TaskName $taskName
            [pscustomobject]@{
                Persona        = $key
                TaskName       = $taskName
                Enabled        = $task.Settings.Enabled
                State          = $task.State
                LastRunTime    = $info.LastRunTime
                NextRunTime    = $info.NextRunTime
                LastTaskResult = $info.LastTaskResult
            } | Format-List
        }
        "on" {
            Enable-ScheduledTask -TaskName $taskName | Out-Null
            Write-Host ("[{0}] Enabled: {1}" -f $key.ToUpper(), $taskName)
        }
        "off" {
            Disable-ScheduledTask -TaskName $taskName | Out-Null
            Write-Host ("[{0}] Disabled: {1}" -f $key.ToUpper(), $taskName)
        }
        "toggle" {
            if ($task.Settings.Enabled) {
                Disable-ScheduledTask -TaskName $taskName | Out-Null
                Write-Host ("[{0}] Toggled OFF: {1}" -f $key.ToUpper(), $taskName)
            }
            else {
                Enable-ScheduledTask -TaskName $taskName | Out-Null
                Write-Host ("[{0}] Toggled ON: {1}" -f $key.ToUpper(), $taskName)
            }
        }
        "run" {
            Start-ScheduledTask -TaskName $taskName
            Write-Host ("[{0}] Started now: {1}" -f $key.ToUpper(), $taskName)
        }
        "monitor" {
            $info = Get-ScheduledTaskInfo -TaskName $taskName
            $lastRecord = Get-LastRunRecord -PersonaValue $key
            $intervalDuration = $task.Triggers[0].Repetition.Interval
            $intervalHours = Parse-IsoDurationHours -Duration $intervalDuration
            $allowedLagHours = if ($intervalHours) { $intervalHours + 1 } else { 9 }

            $deliveryState = "unknown"
            $ageHours = $null
            if ($lastRecord -and $lastRecord.status -eq "sent") {
                $ts = [DateTimeOffset]::Parse($lastRecord.timestamp_utc)
                $ageHours = (([DateTimeOffset]::UtcNow - $ts).TotalHours)
                if ($ageHours -le $allowedLagHours) {
                    $deliveryState = "healthy"
                }
                else {
                    $deliveryState = "late"
                }
            }
            elseif ($lastRecord -and $lastRecord.status -eq "failed") {
                $deliveryState = "failed"
            }

            [pscustomobject]@{
                Persona              = $key
                TaskName             = $taskName
                Enabled              = $task.Settings.Enabled
                State                = $task.State
                LastTaskResult       = $info.LastTaskResult
                LastSchedulerRunTime = $info.LastRunTime
                NextRunTime          = $info.NextRunTime
                LastTrackedStatus    = if ($lastRecord) { $lastRecord.status } else { "none" }
                LastTrackedAtUtc     = if ($lastRecord) { $lastRecord.timestamp_utc } else { "none" }
                LastSendAgeHours     = if ($ageHours -ne $null) { [math]::Round($ageHours, 2) } else { "n/a" }
                Validation           = $deliveryState
            } | Format-List

            if ($lastRecord -and $lastRecord.status -eq "failed" -and $lastRecord.error) {
                Write-Host ("[{0}] Last tracked error: {1}" -f $key.ToUpper(), $lastRecord.error)
            }
        }
    }
}
