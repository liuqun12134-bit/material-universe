param(
    [switch]$SkipDependencyInstall
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$buildRoot = Join-Path $projectRoot ".build"
$venvRoot = Join-Path $buildRoot "venv"
$venvPython = Join-Path $venvRoot "Scripts\python.exe"
$installerBuildRoot = Join-Path $buildRoot "installer"
$distRoot = Join-Path $installerBuildRoot "dist"
$workRoot = Join-Path $installerBuildRoot "pyinstaller"
$specPath = Join-Path $PSScriptRoot "material_universe_installed.spec"
$installerScript = Join-Path $PSScriptRoot "material_universe_installer.iss"
$releaseRoot = Join-Path $projectRoot "发行版"
$installerPath = Join-Path $releaseRoot "素材万象安装程序.exe"
$historyRoot = Join-Path $releaseRoot "历史版本"
$historyPath = Join-Path $releaseRoot "版本记录.csv"
$currentInstallerPath = Join-Path $releaseRoot "当前安装版.json"

function Find-InnoCompiler {
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
        (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe")
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) {
            return $candidate
        }
    }
    $command = Get-Command "ISCC.exe" -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }
    throw "没有找到 Inno Setup 6。请先运行：winget install --id JRSoftware.InnoSetup --exact"
}

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

function Save-CurrentInstaller {
    if (-not (Test-Path -LiteralPath $installerPath)) {
        return
    }

    New-Item -ItemType Directory -Path $historyRoot -Force | Out-Null
    $currentFile = Get-Item -LiteralPath $installerPath
    $version = Get-SafeVersion $currentFile
    $stamp = Get-Date -Format "yyyyMMdd_HHmmss_fff"
    $backupName = "素材万象安装程序_v${version}_${stamp}.exe"
    $backupPath = Join-Path $historyRoot $backupName
    Copy-Item -LiteralPath $installerPath -Destination $backupPath
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
}

if (-not (Test-Path -LiteralPath $venvPython)) {
    python -m venv $venvRoot
}

if (-not $SkipDependencyInstall) {
    & $venvPython -m pip install --disable-pip-version-check --upgrade pip
    & $venvPython -m pip install --disable-pip-version-check `
        -r (Join-Path $projectRoot "skills\video-product-swap\requirements.txt") `
        "pyinstaller>=6.18,<7"
}

$tools = @{}
foreach ($name in @("ffmpeg", "ffprobe", "ffplay")) {
    $command = Get-Command $name -ErrorAction SilentlyContinue
    if (-not $command) {
        throw "缺少 $name，无法制作完整安装版。"
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
New-Item -ItemType Directory -Path $installerBuildRoot -Force | Out-Null

& $venvPython -m PyInstaller `
    --noconfirm `
    --clean `
    --distpath $distRoot `
    --workpath $workRoot `
    $specPath

if ($LASTEXITCODE -ne 0) {
    throw "安装版程序文件构建失败。"
}

$appPath = Join-Path $distRoot "素材万象\素材万象.exe"
if (-not (Test-Path -LiteralPath $appPath)) {
    throw "构建完成，但没有生成预期的素材万象.exe。"
}

$selfTestPath = Join-Path $installerBuildRoot "installed-self-test.json"
$selfTest = Start-Process `
    -FilePath $appPath `
    -ArgumentList @("--portable-self-test", $selfTestPath) `
    -WindowStyle Hidden `
    -Wait `
    -PassThru
if ($selfTest.ExitCode -ne 0) {
    $detail = if (Test-Path -LiteralPath $selfTestPath) {
        Get-Content -LiteralPath $selfTestPath -Raw -Encoding UTF8
    } else {
        "没有生成自检报告。"
    }
    throw "安装版程序自检失败：$detail"
}

$appFile = Get-Item -LiteralPath $appPath
$version = Get-SafeVersion $appFile
$innoCompiler = Find-InnoCompiler
Save-CurrentInstaller

& $innoCompiler "/DMyAppVersion=$version" $installerScript
if ($LASTEXITCODE -ne 0) {
    throw "Windows 安装程序构建失败。"
}
if (-not (Test-Path -LiteralPath $installerPath)) {
    throw "构建完成，但没有生成预期的安装程序。"
}

$installerFile = Get-Item -LiteralPath $installerPath
$installerHash = (Get-FileHash -LiteralPath $installerPath -Algorithm SHA256).Hash
[pscustomobject]@{
    Version = $version
    BuiltAt = $installerFile.LastWriteTime.ToString("yyyy-MM-dd HH:mm:ss")
    SizeBytes = $installerFile.Length
    SHA256 = $installerHash
    FileName = $installerFile.Name
    InstallScope = "CurrentUser"
    DefaultInstallPath = "%LOCALAPPDATA%\Programs\MaterialUniverse"
    WorkspacePath = "%USERPROFILE%\Documents\素材万象"
} | ConvertTo-Json | Set-Content -LiteralPath $currentInstallerPath -Encoding UTF8

Write-Host "安装程序构建完成：$installerPath (version $version)"
