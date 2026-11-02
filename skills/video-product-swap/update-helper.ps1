param([Parameter(Mandatory=$true)][string]$Ticket)
$ErrorActionPreference = 'Stop'
$taskInfo = Get-Content -LiteralPath $Ticket -Raw -Encoding UTF8 | ConvertFrom-Json
$taskReceipt = Join-Path $taskInfo.data_root 'updates\last-install.json'
try {
    $taskParent = Get-Process -Id $taskInfo.parent_pid -ErrorAction SilentlyContinue
    if ($taskParent) { $taskParent.WaitForExit(180000) | Out-Null }
    if ($taskParent -and -not $taskParent.HasExited) { throw '软件尚未退出，已取消覆盖更新。' }
    if ((Get-FileHash -LiteralPath $taskInfo.installer -Algorithm SHA256).Hash.ToLowerInvariant() -ne $taskInfo.sha256) {
        throw '更新文件校验失败。'
    }
    $taskDestination = [IO.Path]::GetFullPath($taskInfo.install_dir)
    if (-not (Test-Path -LiteralPath (Join-Path $taskDestination 'unins000.exe'))) { throw '原安装目录无法确认。' }
    $taskArguments = @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/CLOSEAPPLICATIONS', ('/DIR="' + $taskDestination + '"'))
    $taskInstaller = Start-Process -FilePath $taskInfo.installer -ArgumentList $taskArguments -WindowStyle Hidden -Wait -PassThru
    if ($taskInstaller.ExitCode -ne 0) { throw ('安装未完成，返回码：' + $taskInstaller.ExitCode) }
    @{version=$taskInfo.version;success=$true;finished=(Get-Date).ToString('o')} | ConvertTo-Json | Set-Content -LiteralPath $taskReceipt -Encoding UTF8
    Start-Process -FilePath (Join-Path $taskDestination '素材万象.exe') -WorkingDirectory $taskDestination
} catch {
    @{version=$taskInfo.version;success=$false;error=$_.Exception.Message;finished=(Get-Date).ToString('o')} | ConvertTo-Json | Set-Content -LiteralPath $taskReceipt -Encoding UTF8
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show(('更新未完成。原素材库已保留。' + [Environment]::NewLine + $_.Exception.Message), '素材万象更新') | Out-Null
    exit 1
}
