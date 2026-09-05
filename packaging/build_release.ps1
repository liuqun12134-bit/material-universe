$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$buildRoot = Join-Path $projectRoot ".build"
$venvRoot = Join-Path $buildRoot "venv"
$venvPython = Join-Path $venvRoot "Scripts\python.exe"
$releaseFolderName = -join [char[]](0x53D1, 0x884C, 0x7248)
$productName = -join [char[]](0x7D20, 0x6750, 0x4E07, 0x8C61)
$releaseRoot = Join-Path $projectRoot $releaseFolderName
$specPath = Join-Path $PSScriptRoot "material_universe.spec"
$exePath = Join-Path $releaseRoot ($productName + ".exe")
$historyRoot = Join-Path $releaseRoot "历史版本"
$historyPath = Join-Path $releaseRoot "版本记录.csv"
$currentVersionPath = Join-Path $releaseRoot "当前版本.json"

function Get-SafeVersion([System.IO.FileInfo]$File) {
    $version = $File.VersionInfo.ProductVersion
    if (-not $version) {
        $version = $File.VersionInfo.FileVersion
    }
    if (-not $version) {
        $version = "unknown"
    }
    return ($version -replace '[^0-9A-Za-z._-]', '_')
}

function Save-CurrentRelease {
    if (-not (Test-Path -LiteralPath $exePath)) {
        return
    }

    New-Item -ItemType Directory -Path $historyRoot -Force | Out-Null
    $currentFile = Get-Item -LiteralPath $exePath
    $version = Get-SafeVersion $currentFile
    $stamp = Get-Date -Format "yyyyMMdd_HHmmss_fff"
    $backupName = "${productName}_v${version}_${stamp}.exe"
    $backupPath = Join-Path $historyRoot $backupName
    Copy-Item -LiteralPath $exePath -Destination $backupPath
    $backupFile = Get-Item -LiteralPath $backupPath
    $hash = (Get-FileHash -LiteralPath $backupPath -Algorithm SHA256).Hash
    $record = [pscustomobject]@{
        ArchivedAt = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
        Version = $version
        OriginalLastWriteTime = $currentFile.LastWriteTime.ToString("yyyy-MM-dd HH:mm:ss")
        SizeBytes = $backupFile.Length
        SHA256 = $hash
        FileName = $backupName
    }
    if (Test-Path -LiteralPath $historyPath) {
        $record | Export-Csv -LiteralPath $historyPath -NoTypeInformation -Encoding UTF8 -Append
    } else {
        $record | Export-Csv -LiteralPath $historyPath -NoTypeInformation -Encoding UTF8
    }
    Write-Host "Archived previous release: $backupPath"
}

if (-not (Test-Path -LiteralPath $venvPython)) {
    python -m venv $venvRoot
}

& $venvPython -m pip install --disable-pip-version-check --upgrade pip
& $venvPython -m pip install --disable-pip-version-check `
    -r (Join-Path $projectRoot "skills\video-product-swap\requirements.txt") `
    "pyinstaller>=6.18,<7"

$tools = @{}
foreach ($name in @("ffmpeg", "ffprobe", "ffplay")) {
    $command = Get-Command $name -ErrorAction SilentlyContinue
    if (-not $command) {
        throw "Missing $name; cannot build the complete single-file edition."
    }
    $resolved = (Get-Item -LiteralPath $command.Source).Target
    if (-not $resolved) {
        $resolved = $command.Source
    }
    $tools[$name] = $resolved
}

$env:MATERIAL_UNIVERSE_FFMPEG = $tools["ffmpeg"]
$env:MATERIAL_UNIVERSE_FFPROBE = $tools["ffprobe"]
$env:MATERIAL_UNIVERSE_FFPLAY = $tools["ffplay"]

New-Item -ItemType Directory -Path $releaseRoot -Force | Out-Null
Save-CurrentRelease
& $venvPython -m PyInstaller `
    --noconfirm `
    --clean `
    --distpath $releaseRoot `
    --workpath (Join-Path $buildRoot "pyinstaller") `
    $specPath

if ($LASTEXITCODE -ne 0) {
    throw "The executable build failed."
}

if (-not (Test-Path -LiteralPath $exePath)) {
    throw "The build completed without producing the expected executable."
}

$builtFile = Get-Item -LiteralPath $exePath
$builtVersion = Get-SafeVersion $builtFile
$builtHash = (Get-FileHash -LiteralPath $exePath -Algorithm SHA256).Hash
[pscustomobject]@{
    Version = $builtVersion
    BuiltAt = $builtFile.LastWriteTime.ToString("yyyy-MM-dd HH:mm:ss")
    SizeBytes = $builtFile.Length
    SHA256 = $builtHash
    FileName = $builtFile.Name
} | ConvertTo-Json | Set-Content -LiteralPath $currentVersionPath -Encoding UTF8

Write-Host "Build completed: $exePath (version $builtVersion)"
