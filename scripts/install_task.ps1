<#
.SYNOPSIS
    为「音乐下载自动化管理」（包名 music_sync）注册 / 更新 / 删除 Windows 计划任务。

.DESCRIPTION
    用 schtasks.exe 注册任务（不用 New-ScheduledTask*，老系统也认）。
    注意：schtasks 的 /SC 系列开关无法设置「工作目录」和「错过任务后补跑」，
    所以这里改为生成任务 XML 再 `schtasks /Create /XML`——仍然是 schtasks.exe，
    但能一次性把 WorkingDirectory = 项目根、StartWhenAvailable = true 都设好。

    脚本是幂等的：每次都先 /Delete /F 再重建，重复跑不会报「任务已存在」。

.EXAMPLE
    # 每周日 21:00（参数都不给时，读 config.json 里的 schedule 设置）
    .\scripts\install_task.ps1 -Weekly -Weekday Sunday -Time 21:00

.EXAMPLE
    # 每 24 小时跑一次
    .\scripts\install_task.ps1 -Hours 24

.EXAMPLE
    .\scripts\install_task.ps1 -RunNow
    .\scripts\install_task.ps1 -Remove
#>
[CmdletBinding()]
param(
    # config.json 路径，默认 ~\.music-sync\config.json（可用 MUSIC_SYNC_HOME 覆盖）
    [string]$Config = "",

    # 计划任务名（要和设置界面里的「计划任务名称」一致）
    [string]$TaskName = "MusicSync",

    # 触发方式：三选一，都不给就读配置文件
    [switch]$Weekly,
    [string]$Weekday = "",
    [string]$Time = "",
    [switch]$Daily,
    [int]$Hours = 0,

    # 手动指定解释器（默认自动找 python.exe 并换成同目录的 pythonw.exe）
    [string]$Python = "",

    [switch]$Remove,
    [switch]$RunNow
)

$ErrorActionPreference = "Stop"

function Write-Step($msg) { Write-Host "[执行] $msg" }
function Write-Ok($msg)   { Write-Host "[成功] $msg" -ForegroundColor Green }
function Write-Info($msg) { Write-Host "[信息] $msg" -ForegroundColor Cyan }
function Fail($msg) {
    Write-Host "[失败] $msg" -ForegroundColor Red
    exit 1
}

# schtasks 失败时会往 stderr 写东西，而 $ErrorActionPreference=Stop 会把它当异常中断脚本，
# 所以统一走这个包装：只取退出码和输出文字，不让它抛异常。
function Invoke-SchTasks {
    param([string[]]$CmdArgs)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & schtasks.exe @CmdArgs 2>&1
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
    return [pscustomobject]@{ Code = $code; Output = (($output | Out-String).Trim()) }
}

# ---------------------------------------------------------------- 路径

$ProjectRoot = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path (Join-Path $ProjectRoot "music_sync"))) {
    Fail "项目根目录不正确：$ProjectRoot（脚本要放在 <项目根>\scripts\ 下）"
}

if (-not $Config) {
    $home_ = $env:MUSIC_SYNC_HOME
    if (-not $home_) { $home_ = Join-Path $env:USERPROFILE ".music-sync" }
    $Config = Join-Path $home_ "config.json"
}
$Config = [System.IO.Path]::GetFullPath($Config)
Write-Info "项目根目录：$ProjectRoot"
Write-Info "配置文件：$Config"

# ---------------------------------------------------------------- 解释器

function Resolve-Pythonw {
    param([string]$Hint)

    $candidates = New-Object System.Collections.ArrayList
    if ($Hint) { [void]$candidates.Add($Hint) }

    # 微软商店的 python.exe 是个占位符，必须排除
    foreach ($cmd in (Get-Command python.exe -All -ErrorAction SilentlyContinue)) {
        if ($cmd.Source -notlike "*WindowsApps*") { [void]$candidates.Add($cmd.Source) }
    }
    $pyLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        try {
            $real = & py -3 -c "import sys; print(sys.executable)" 2>$null
            if ($real) { [void]$candidates.Add($real.Trim()) }
        } catch { }
    }
    [void]$candidates.Add((Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\python.exe"))
    [void]$candidates.Add((Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"))

    foreach ($exe in $candidates) {
        if (-not $exe) { continue }
        if (-not (Test-Path $exe)) { continue }
        $pyw = Join-Path (Split-Path -Parent $exe) "pythonw.exe"   # 无控制台窗口
        if (Test-Path $pyw) { return $pyw }
        return $exe
    }
    Fail "找不到 Python，请先安装 Python 3.9+ 并勾选「Add Python to PATH」，或用 -Python 指定 python.exe 的完整路径"
}

if ($Remove) {
    Write-Step "删除计划任务 $TaskName"
    $result = Invoke-SchTasks @("/Delete", "/TN", $TaskName, "/F")
    if ($result.Code -ne 0) {
        Write-Info "没有找到该任务（或已经被删掉了）：$($result.Output)"
    } else {
        Write-Ok "已删除计划任务 $TaskName"
    }
    Write-Info "配置文件 $Config 保留未动。"
    exit 0
}

if (-not (Test-Path $Config)) {
    Fail "找不到配置文件 $Config。请先运行 `"python -m music_sync settings`" 保存一次设置。"
}

$Pythonw = Resolve-Pythonw -Hint $Python
Write-Info "解释器：$Pythonw"

# ---------------------------------------------------------------- 从配置读默认值

$cfg = $null
try {
    $cfg = Get-Content -Raw -Encoding UTF8 -Path $Config | ConvertFrom-Json
} catch {
    Fail "config.json 解析失败：$($_.Exception.Message)"
}
$sched = $cfg.schedule

# 触发方式：显式开关优先，否则用配置文件里的
$mode = ""
if ($Weekly) { $mode = "weekly" }
elseif ($Daily) { $mode = "daily" }
elseif ($Hours -gt 0) { $mode = "hours" }

$switchCount = @($Weekly.IsPresent, $Daily.IsPresent, ($Hours -gt 0)) | Where-Object { $_ }
if ($switchCount.Count -gt 1) {
    Fail "-Weekly / -Daily / -Hours 只能选一个"
}
if (-not $mode) {
    $mode = "weekly"
    if ($sched -and $sched.mode) { $mode = [string]$sched.mode }
    Write-Info "未指定触发方式，采用配置文件里的 mode=$mode"
}

if (-not $Time -and $sched -and $sched.time) { $Time = [string]$sched.time }
if (-not $Time) { $Time = "21:00" }
if (-not $Weekday -and $sched -and $sched.weekday) { $Weekday = [string]$sched.weekday }
if (-not $Weekday) { $Weekday = "sunday" }
if ($Hours -le 0) {
    if ($sched -and $sched.hours) { $Hours = [int]$sched.hours }
}
if ($Hours -le 0) { $Hours = 168 }

if (-not $PSBoundParameters.ContainsKey('TaskName') -and $sched -and $sched.task_name) {
    $TaskName = [string]$sched.task_name
}

# ---------------------------------------------------------------- 校验

if (-not [regex]::IsMatch($Time, '^([01]?\d|2[0-3]):[0-5]\d$')) {
    Fail "时间格式不对：$Time（要写成 HH:MM，例如 21:00）"
}
$TimeNormalized = ([datetime]::ParseExact($Time, "H:mm", $null)).ToString("HH:mm")
if ($Hours -lt 1 -or $Hours -gt 8760) {
    Fail "间隔小时数要在 1~8760 之间，现在是：$Hours"
}

$validWeekdays = @{
    "monday" = "Monday"; "tuesday" = "Tuesday"; "wednesday" = "Wednesday"
    "thursday" = "Thursday"; "friday" = "Friday"; "saturday" = "Saturday"; "sunday" = "Sunday"
}
$weekdayKey = $Weekday.Trim().ToLower()
if (-not $validWeekdays.ContainsKey($weekdayKey)) {
    Fail "星期写错了：$Weekday（可选 Monday~Sunday）"
}
$weekdayXml = $validWeekdays[$weekdayKey]

$stamp = (Get-Date).ToString("yyyy-MM-dd") + "T" + $TimeNormalized + ":00"
if ($mode -eq "hours") {
    # 每隔 N 小时：从下一分钟起算，避免 StartBoundary 落在很久以前
    $stamp = (Get-Date).AddMinutes(1).ToString("yyyy-MM-ddTHH:mm:ss")
}

# ---------------------------------------------------------------- 组装任务 XML

function Escape-Xml($text) {
    return [System.Security.SecurityElement]::Escape([string]$text)
}

$command = Escape-Xml $Pythonw
# 注意：--config 是全局参数，必须放在子命令 sync 前面，否则 argparse 会报
# "unrecognized arguments: --config"
$arguments = Escape-Xml ("-m music_sync --config """ + $Config + """ sync --gui")
$workdir = Escape-Xml $ProjectRoot
$description = Escape-Xml "音乐下载自动化管理：定时把歌单里的歌补齐到本地曲库"

switch ($mode) {
    "weekly" {
        $triggerXml = @"
    <CalendarTrigger>
      <StartBoundary>$stamp</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByWeek>
        <WeeksInterval>1</WeeksInterval>
        <DaysOfWeek><$weekdayXml /></DaysOfWeek>
      </ScheduleByWeek>
    </CalendarTrigger>
"@
        $triggerText = "每周 $weekdayXml $TimeNormalized"
    }
    "daily" {
        $triggerXml = @"
    <CalendarTrigger>
      <StartBoundary>$stamp</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay>
        <DaysInterval>1</DaysInterval>
      </ScheduleByDay>
    </CalendarTrigger>
"@
        $triggerText = "每天 $TimeNormalized"
    }
    "hours" {
        $triggerXml = @"
    <TimeTrigger>
      <StartBoundary>$stamp</StartBoundary>
      <Enabled>true</Enabled>
      <Repetition>
        <Interval>PT${Hours}H</Interval>
        <Duration>P3650D</Duration>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
    </TimeTrigger>
"@
        $triggerText = "每 $Hours 小时"
    }
    default { Fail "不认识的触发方式：$mode" }
}

$taskXml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Author>MusicSync</Author>
    <Description>$description</Description>
  </RegistrationInfo>
  <Triggers>
$triggerXml
  </Triggers>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT2H</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>$command</Command>
      <Arguments>$arguments</Arguments>
      <WorkingDirectory>$workdir</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@

$tmpXml = Join-Path $env:TEMP ("musicsync-task-" + [guid]::NewGuid().ToString("N") + ".xml")
# schtasks 对 UTF-16 最稳，中文描述不会乱码
[System.IO.File]::WriteAllText($tmpXml, $taskXml, [System.Text.Encoding]::Unicode)

Write-Info "任务名：$TaskName"
Write-Info "触发方式：$triggerText"
Write-Info "执行命令：`"$Pythonw`" -m music_sync sync --gui --config `"$Config`""
Write-Info "工作目录：$ProjectRoot"

# ---------------------------------------------------------------- 先删后建（幂等）

Write-Step "删除可能存在的旧任务"
$deleteResult = Invoke-SchTasks @("/Delete", "/TN", $TaskName, "/F")
if ($deleteResult.Code -eq 0) { Write-Ok "已删除旧任务" } else { Write-Info "旧任务不存在，直接新建" }

Write-Step "按 XML 创建任务"
$createResult = Invoke-SchTasks @("/Create", "/TN", $TaskName, "/XML", $tmpXml, "/F")
if ($createResult.Code -ne 0) {
    Remove-Item $tmpXml -ErrorAction SilentlyContinue
    Fail "创建失败：$($createResult.Output)`n常见原因：任务名被系统任务占用、需要管理员权限，或组策略禁止创建计划任务。"
}
Write-Ok "任务已注册"

Write-Step "回读任务确认"
$queryResult = Invoke-SchTasks @("/Query", "/TN", $TaskName, "/FO", "LIST")
if ($queryResult.Code -ne 0) {
    Remove-Item $tmpXml -ErrorAction SilentlyContinue
    Fail "注册后查不到任务 $TaskName：$($queryResult.Output)"
}
$queryResult.Output -split "`r?`n" | ForEach-Object { Write-Host "    $_" }
Remove-Item $tmpXml -ErrorAction SilentlyContinue

if ($RunNow) {
    Write-Step "立即运行一次"
    $runResult = Invoke-SchTasks @("/Run", "/TN", $TaskName)
    if ($runResult.Code -ne 0) { Fail "启动失败：$($runResult.Output)" }
    Write-Ok "已启动，右下角提示窗稍后出现"
}

Write-Host ""
Write-Ok "完成：任务 $TaskName 已按「$triggerText」运行"
Write-Info "看状态：schtasks /Query /TN `"$TaskName`" /V /FO LIST"
Write-Info "卸载：  .\scripts\install_task.ps1 -TaskName `"$TaskName`" -Remove"
exit 0
