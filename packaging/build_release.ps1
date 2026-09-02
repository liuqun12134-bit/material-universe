$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$buildRoot = Join-Path $projectRoot ".build"
$venvRoot = Join-Path $buildRoot "venv"
$venvPython = Join-Path $venvRoot "Scripts\python.exe"
$releaseFolderName = -join [char[]](0x53D1, 0x884C, 0x7248)
$productName = -join [char[]](0x7D20, 0x6750, 0x4E07, 0x8C61)
$releaseRoot = Join-Path $projectRoot $releaseFolderName
$specPath = Join-Path $PSScriptRoot "material_universe.spec"

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
& $venvPython -m PyInstaller `
    --noconfirm `
    --clean `
    --distpath $releaseRoot `
    --workpath (Join-Path $buildRoot "pyinstaller") `
    $specPath

if ($LASTEXITCODE -ne 0) {
    throw "The executable build failed."
}

$exePath = Join-Path $releaseRoot ($productName + ".exe")
if (-not (Test-Path -LiteralPath $exePath)) {
    throw "The build completed without producing the expected executable."
}

Write-Host "Build completed: $exePath"
