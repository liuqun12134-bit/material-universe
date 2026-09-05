param(
    [switch]$ListOnly
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$releaseRoot = Join-Path $projectRoot "发行版"
$historyRoot = Join-Path $releaseRoot "历史版本"
$activePath = Join-Path $releaseRoot "素材万象.exe"

if (-not (Test-Path -LiteralPath $historyRoot)) {
    throw "没有找到历史版本文件夹。"
}

$backups = @(Get-ChildItem -LiteralPath $historyRoot -Filter "*.exe" -File |
    Sort-Object LastWriteTime -Descending)
if ($backups.Count -eq 0) {
    throw "目前没有可回退的历史版本。"
}

Write-Host ""
Write-Host "素材万象历史版本"
Write-Host "----------------"
for ($index = 0; $index -lt $backups.Count; $index++) {
    $file = $backups[$index]
    $version = $file.VersionInfo.ProductVersion
    if (-not $version) {
        $version = "unknown"
    }
    $size = [math]::Round($file.Length / 1MB, 1)
    Write-Host ("[{0}] v{1}  {2}  {3} MB" -f ($index + 1), $version, $file.LastWriteTime.ToString("yyyy-MM-dd HH:mm:ss"), $size)
}

if ($ListOnly) {
    exit 0
}

$selectionText = Read-Host "请输入要恢复的编号"
$selection = 0
if (-not [int]::TryParse($selectionText, [ref]$selection)) {
    throw "输入的编号无效。"
}
if ($selection -lt 1 -or $selection -gt $backups.Count) {
    throw "输入的编号不在列表中。"
}

$selected = $backups[$selection - 1]
$confirmation = Read-Host ("确认恢复 {0} 吗？输入 Y 继续" -f $selected.Name)
if ($confirmation -notin @("Y", "y")) {
    Write-Host "已取消，没有修改发行版。"
    exit 0
}

if (Get-Process -Name "素材万象" -ErrorAction SilentlyContinue) {
    throw "请先关闭正在运行的素材万象，再执行回退。"
}

if (Test-Path -LiteralPath $activePath) {
    $activeFile = Get-Item -LiteralPath $activePath
    $activeVersion = $activeFile.VersionInfo.ProductVersion
    if (-not $activeVersion) {
        $activeVersion = "unknown"
    }
    $safeVersion = $activeVersion -replace '[^0-9A-Za-z._-]', '_'
    $stamp = Get-Date -Format "yyyyMMdd_HHmmss_fff"
    $safetyName = "素材万象_回退前_v${safeVersion}_${stamp}.exe"
    Copy-Item -LiteralPath $activePath -Destination (Join-Path $historyRoot $safetyName)
}

Copy-Item -LiteralPath $selected.FullName -Destination $activePath -Force
Write-Host ("已恢复：{0}" -f $selected.Name)
Write-Host ("当前程序：{0}" -f $activePath)
