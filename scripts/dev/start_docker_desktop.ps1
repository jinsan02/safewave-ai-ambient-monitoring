# Docker Desktop(Windows) 안전 시작
#
# 증상 예:
#   initializing Inference manager: ... remove ...\Docker\run\dockerInference
#   initializing Secrets Engine:  ... remove ...\docker-secrets-engine\engine.sock
#   -> "The file cannot be accessed by the system." 후 Docker Desktop 종료
# 원인: 이 PC에서는 Docker가 만든 AF_UNIX 소켓(재분석 지점)이 종료 뒤 일반 권한으로
#       지워지지 않는다(Win32 1920). 다음 시작 때 같은 경로에 소켓을 다시 만들지 못해 실패한다.
#       공장 초기화는 설정만 되돌리고 이 파일은 지우지 못하므로 해결되지 않는다.
# 조치: 시작 전에 소켓이 남은 폴더를 이름만 바꿔 비켜 두고, Docker가 새 폴더를 만들게 한다.
#       -Purge: 관리자 PowerShell에서 비켜 둔 폴더의 소켓까지 삭제한다.
#       -Restart: 떠 있는(오류 창 포함) Docker Desktop을 먼저 종료한다.
#
# 사용: powershell -ExecutionPolicy Bypass -File scripts\dev\start_docker_desktop.ps1 [-Restart] [-Purge]

param([switch]$Restart, [switch]$Purge)

$ErrorActionPreference = "Stop"
$exe = "C:\Program Files\Docker\Docker\Docker Desktop.exe"
$socketDirs = @(
    (Join-Path $env:LOCALAPPDATA "Docker\run"),
    (Join-Path $env:LOCALAPPDATA "docker-secrets-engine")
)
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"

$running = Get-Process -Name "Docker Desktop", "com.docker.backend", "com.docker.build" -ErrorAction SilentlyContinue
if ($running) {
    if (-not $Restart) {
        Write-Host "Docker Desktop is already running. Use -Restart to stop it first."
        exit 0
    }
    $running | Stop-Process -Force
    Start-Sleep -Seconds 3
    Write-Host "Stopped running Docker Desktop processes."
}

# 1) 소켓이 남은 폴더를 비켜 둔다 (wsl 데이터 폴더는 건드리지 않음)
foreach ($dir in $socketDirs) {
    if (-not (Test-Path $dir)) { continue }
    $stale = @(Get-ChildItem -Force $dir -Recurse -Depth 1 -ErrorAction SilentlyContinue |
        Where-Object { $_.Attributes -match "ReparsePoint" })
    if ($stale.Count -gt 0) {
        $newName = (Split-Path $dir -Leaf) + ".stale-" + $stamp
        Rename-Item $dir $newName
        Write-Host "Moved $($stale.Count) stale socket(s): $dir -> $newName"
    }
}

# 2) 실행 중 자동 업데이트는 백엔드를 재시작시켜 방금 만든 소켓 때문에 다시 실패한다(09-17 확인).
#    Docker AI도 쓰지 않으므로 끈다. 단, Inference manager는 이 값과 무관하게 시작된다.
$settings = Join-Path $env:APPDATA "Docker\settings-store.json"
if (Test-Path $settings) {
    $json = [IO.File]::ReadAllText($settings).TrimStart([char]0xFEFF) | ConvertFrom-Json
    $changed = @()
    foreach ($name in "AutoDownloadUpdates", "EnableDockerAI") {
        if ($json.PSObject.Properties.Name -notcontains $name) {
            $json | Add-Member -NotePropertyName $name -NotePropertyValue $false
            $changed += $name
        } elseif ($json.$name -ne $false) {
            $json.$name = $false
            $changed += $name
        }
    }
    if ($changed) {
        Copy-Item $settings "$settings.bak" -Force
        [IO.File]::WriteAllText($settings, ($json | ConvertTo-Json -Depth 20),
            (New-Object System.Text.UTF8Encoding($false)))
        Write-Host "Set $($changed -join ', ') = false (backup: settings-store.json.bak)"
    }
}

# 3) 관리자 권한이면 비켜 둔 폴더 정리
if ($Purge) {
    $admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
        ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $admin) {
        Write-Warning "-Purge needs an elevated PowerShell. Skipped."
    } else {
        $parents = $socketDirs | ForEach-Object { Split-Path $_ -Parent } | Select-Object -Unique
        foreach ($parent in $parents) {
            Get-ChildItem -Force $parent -Directory | Where-Object { $_.Name -match "\.stale" } | ForEach-Object {
                Get-ChildItem -Force $_.FullName | ForEach-Object {
                    fsutil reparsepoint delete $_.FullName | Out-Null
                    Remove-Item -Force $_.FullName -ErrorAction SilentlyContinue
                }
                Remove-Item -Force $_.FullName -ErrorAction SilentlyContinue
                if (Test-Path $_.FullName) { Write-Warning "Could not remove $($_.FullName)" }
                else { Write-Host "Removed $($_.Name)" }
            }
        }
    }
}

Start-Process $exe
Write-Host "Docker Desktop started. Quit it from the tray menu (force-kill leaves sockets behind)."
