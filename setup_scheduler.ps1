# setup_scheduler.ps1
# Registers a Windows Task Scheduler task to run a persona digest every N hours.
# Run this script ONCE as Administrator:
#   Right-click PowerShell -> "Run as Administrator"
#   cd "C:\Users\sanyy\OneDrive\Desktop\article_agent"
#   .\setup_scheduler.ps1 -TaskName "AIDigestBot" -Persona "ai"

param(
    [string]$TaskName = "AIDigestAgent",
    [string]$Persona = "ai",
    [int]$IntervalHours = 8,
    [string]$ConfigPath = ""
)

$ScriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonExe   = Join-Path $ScriptDir ".venv\Scripts\python.exe"
$AgentScript = Join-Path $ScriptDir "src\ai_digest_agent.py"

if (-not $ConfigPath) {
    $ConfigPath = Join-Path $ScriptDir "config\personas\$Persona.yaml"
    if (-not (Test-Path $ConfigPath)) {
        $ConfigPath = Join-Path $ScriptDir "config\feeds.yaml"
    }
}

if (-not (Test-Path $PythonExe)) {
    Write-Error "Virtual-env Python not found at: $PythonExe"
    Write-Host  "Run: python -m venv .venv  then  pip install -r requirements.txt"
    exit 1
}

# Remove existing task if present
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    try {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
        Write-Host "Removed existing task '$TaskName'."
    }
    catch {
        Write-Error "Failed to remove existing task '$TaskName': $($_.Exception.Message)"
        exit 1
    }
}

# Action: run python with --once flag
$Action = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument "`"$AgentScript`" --once --persona `"$Persona`" --config `"$ConfigPath`"" `
    -WorkingDirectory $ScriptDir

# Trigger: every 8 hours, starting now
$StartTime = (Get-Date).AddMinutes(1)   # first run in 1 minute
$Trigger   = New-ScheduledTaskTrigger -RepetitionInterval (New-TimeSpan -Hours $IntervalHours) `
                                       -RepetitionDuration (New-TimeSpan -Days 3650) `
                                       -Once -At $StartTime

# Run whether or not user is logged on, with highest privileges
$Settings  = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew

$Principal = New-ScheduledTaskPrincipal `
    -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType S4U `
    -RunLevel Highest

try {
    Register-ScheduledTask `
        -TaskName  $TaskName `
        -Action    $Action `
        -Trigger   $Trigger `
        -Settings  $Settings `
        -Principal $Principal `
        -Description "Persona digest agent ($Persona) - runs every $IntervalHours hours and sends Telegram notification" `
        -ErrorAction Stop | Out-Null
    $registrationMode = "S4U principal (run whether logged on or not)"
}
catch {
    Write-Warning "Initial registration failed: $($_.Exception.Message)"
    Write-Host "Retrying with current-user interactive registration (no admin required)..."

    try {
        Register-ScheduledTask `
            -TaskName  $TaskName `
            -Action    $Action `
            -Trigger   $Trigger `
            -Settings  $Settings `
            -Description "Persona digest agent ($Persona) - runs every $IntervalHours hours and sends Telegram notification" `
            -ErrorAction Stop | Out-Null
        $registrationMode = "Current user interactive"
    }
    catch {
        Write-Error "Task registration failed: $($_.Exception.Message)"
        Write-Host "Tip: Run PowerShell as Administrator and re-run this script for S4U mode."
        exit 1
    }
}

Write-Host ""
Write-Host "Task '$TaskName' registered successfully."
Write-Host "Persona  : $Persona"
Write-Host "Config   : $ConfigPath"
Write-Host "Mode     : $registrationMode"
Write-Host "First run: $($StartTime.ToString('yyyy-MM-dd HH:mm'))"
Write-Host "Repeats  : every $IntervalHours hours"
Write-Host ""
Write-Host 'Useful commands:'
Write-Host ('  Start now   : Start-ScheduledTask -TaskName ''{0}''' -f $TaskName)
Write-Host ('  Check status: Get-ScheduledTask -TaskName ''{0}'' | Select-Object -ExpandProperty State' -f $TaskName)
Write-Host ('  Remove task : Unregister-ScheduledTask -TaskName ''{0}'' -Confirm:$false' -f $TaskName)
