# Hidden Windows tray controller for Parallel (页间).
# Launched by start.vbs so no terminal window has to stay open.
$ErrorActionPreference = 'Stop'
$script:root = Split-Path -Parent $MyInvocation.MyCommand.Path
$script:url = 'http://127.0.0.1:8765'
$script:python = Join-Path $script:root '.venv\Scripts\python.exe'
$script:logDirectory = Join-Path $script:root 'tmp'
$script:logFile = Join-Path $script:logDirectory 'parallel.log'
$script:errorLogFile = Join-Path $script:logDirectory 'parallel.err.log'
$script:serverProcess = $null
$script:notify = $null
$script:trayIcon = $null
$script:context = $null

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

function New-TrayIcon {
    $iconPath = Join-Path $script:root 'static\parallel.ico'
    try {
        return [System.Drawing.Icon]::new($iconPath, [System.Windows.Forms.SystemInformation]::SmallIconSize)
    } catch {
        # A missing/damaged optional icon must not stop the local service.
        try {
            New-Item -ItemType Directory -Force -Path $script:logDirectory | Out-Null
            [System.IO.File]::AppendAllText($script:errorLogFile, "托盘图标加载失败：$($_.Exception.Message)`r`n")
        } catch {}
        return [System.Drawing.SystemIcons]::Information.Clone()
    }
}

function Test-Server {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri ($script:url + '/api/session') -TimeoutSec 1
        return ($response.StatusCode -eq 200 -and $response.Content -match 'token')
    } catch {
        return $false
    }
}

function Show-Message([string]$message) {
    [System.Windows.Forms.MessageBox]::Show($message, '页间 Parallel') | Out-Null
}

function Start-Server {
    if (Test-Server) {
        return $true
    }
    if (-not (Test-Path -LiteralPath $script:python)) {
        Show-Message '未找到 Python 运行环境。请先运行 start.bat 完成首次安装。'
        return $false
    }
    New-Item -ItemType Directory -Force -Path $script:logDirectory | Out-Null
    try {
        if (-not $script:serverProcess -or $script:serverProcess.HasExited) {
            $script:serverProcess = Start-Process -FilePath $script:python `
                -ArgumentList @('-m', 'uvicorn', 'app:app', '--host', '127.0.0.1', '--port', '8765') `
                -WorkingDirectory $script:root -WindowStyle Hidden -PassThru `
                -RedirectStandardOutput $script:logFile -RedirectStandardError $script:errorLogFile
        }
    } catch {
        [System.IO.File]::AppendAllText($script:errorLogFile, "启动失败：$($_.Exception.Message)`r`n")
        Show-Message '服务启动失败。请查看 tmp\parallel.err.log。'
        return $false
    }
    for ($attempt = 0; $attempt -lt 80; $attempt++) {
        if (Test-Server) {
            return $true
        }
        if ($script:serverProcess.HasExited) {
            break
        }
        Start-Sleep -Milliseconds 250
    }
    Show-Message '服务启动失败。请查看 tmp\parallel.log 或 tmp\parallel.err.log。'
    return $false
}

function Stop-Server {
    $process = $script:serverProcess
    if ($process -and -not $process.HasExited) {
        try {
            $process.Kill()
            $process.WaitForExit(3000) | Out-Null
        } catch {
            # The process may already be gone.
        }
    }
    $script:serverProcess = $null
    try {
        $listeners = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction Stop
        foreach ($listener in $listeners) {
            Stop-Process -Id $listener.OwningProcess -Force -ErrorAction SilentlyContinue
        }
    } catch {
        # No listener means the service is already stopped.
    }
}

function Open-Workbench {
    if (-not (Test-Server) -and -not (Start-Server)) {
        return
    }
    Start-Process $script:url
}

function Update-Menu {
    $running = Test-Server
    $script:openItem.Enabled = $running
    $script:restartItem.Enabled = $running
    $script:startItem.Enabled = (-not $running)
    $script:stopItem.Enabled = $running
    if ($script:notify) {
        if ($running) {
            $script:notify.Text = '页间 Parallel · 运行中'
        } else {
            $script:notify.Text = '页间 Parallel · 已停止'
        }
    }
}

function Start-And-Open {
    if (Start-Server) {
        Start-Process $script:url
        if ($script:notify) {
            $script:notify.ShowBalloonTip(2500, '页间 Parallel', '服务已启动，右键托盘图标可停止或退出。', [System.Windows.Forms.ToolTipIcon]::Info)
        }
    }
}

function Restart-Server {
    Stop-Server
    Start-And-Open
}

function Exit-Tray {
    Stop-Server
    if ($script:notify) {
        $script:notify.Visible = $false
        $script:notify.Dispose()
        $script:notify = $null
    }
    [System.Windows.Forms.Application]::Exit()
}

try {
    # A second launch should just open the running workbench, not add another icon.
    $existing = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -and $_.CommandLine -match '-File\s+.*tray\.ps1' }
    if ($existing) {
        if (-not (Test-Server)) {
            Start-Server | Out-Null
        }
        Start-Process $script:url
        exit 0
    }

    $script:notify = New-Object System.Windows.Forms.NotifyIcon
    $script:trayIcon = New-TrayIcon
    $script:notify.Icon = $script:trayIcon
    $script:notify.Text = '页间 Parallel'
    $script:notify.Visible = $true

    $menu = New-Object System.Windows.Forms.ContextMenuStrip
    $script:openItem = $menu.Items.Add('打开工作台')
    $script:restartItem = $menu.Items.Add('重新启动服务')
    $script:startItem = $menu.Items.Add('启动服务')
    $script:stopItem = $menu.Items.Add('停止服务')
    $menu.Items.Add('-') | Out-Null
    $exitItem = $menu.Items.Add('停止并退出')
    $menu.add_Opening({ Update-Menu })
    $script:openItem.add_Click({ Open-Workbench })
    $script:restartItem.add_Click({ Restart-Server })
    $script:startItem.add_Click({ Start-And-Open })
    $script:stopItem.add_Click({
        Stop-Server
        if ($script:notify) {
            $script:notify.ShowBalloonTip(2000, '页间 Parallel', '服务已停止，可右键托盘图标重新启动。', [System.Windows.Forms.ToolTipIcon]::Info)
        }
    })
    $exitItem.add_Click({ Exit-Tray })
    $script:notify.ContextMenuStrip = $menu
    $script:notify.add_MouseDoubleClick({ Open-Workbench })

    # An ApplicationContext keeps the message loop alive without any window.
    $script:context = New-Object System.Windows.Forms.ApplicationContext

    if (Start-Server) {
        Start-Process $script:url
        $script:notify.ShowBalloonTip(2500, '页间 Parallel', '服务已启动，右键托盘图标可停止或退出。', [System.Windows.Forms.ToolTipIcon]::Info)
    }
    [System.Windows.Forms.Application]::Run($script:context)
} catch {
    try { [System.IO.File]::AppendAllText($script:errorLogFile, "托盘启动失败：$($_.Exception.Message)`r`n") } catch {}
    try { Stop-Server } catch {}
    try { Show-Message "托盘启动失败：$($_.Exception.Message)`n请查看 tmp\parallel.err.log。" } catch {}
    exit 1
} finally {
    if ($script:notify) {
        $script:notify.Visible = $false
        $script:notify.Dispose()
    }
    if ($script:trayIcon) {
        $script:trayIcon.Dispose()
    }
}
