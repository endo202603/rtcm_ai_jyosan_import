$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonExe = "C:\Users\yusuke-endo\AppData\Local\Python\pythoncore-3.14-64\python.exe"
$BuildWorkDir = Join-Path $env:TEMP ("rtcm_ai_jyosan_import-build-" + [Guid]::NewGuid().ToString("N"))

Push-Location $ProjectDir
try {
    & $PythonExe -m PyInstaller `
        --noconfirm `
        --clean `
        --onefile `
        --console `
        --name rtcm_ai_jyosan_import `
        --distpath dist `
        --workpath $BuildWorkDir `
        --specpath build `
        --collect-all playwright `
        --collect-all oracledb `
        --collect-all cryptography `
        --hidden-import win32cred `
        --hidden-import pywintypes `
        watch_folder.py

    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE"
    }

    $OutputDir = Join-Path $ProjectDir "dist\rtcm_ai_jyosan_import"
    New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
    $BuiltExe = Join-Path $ProjectDir "dist\rtcm_ai_jyosan_import.exe"
    $OutputExe = Join-Path $OutputDir "rtcm_ai_jyosan_import.exe"
    try {
        Copy-Item -Force -LiteralPath $BuiltExe -Destination $OutputExe
    }
    catch [System.IO.IOException] {
        $OutputExe = Join-Path $OutputDir "rtcm_ai_jyosan_import_new.exe"
        Copy-Item -Force -LiteralPath $BuiltExe -Destination $OutputExe
        Write-Warning "The existing EXE is running. Created the replacement as $OutputExe"
    }
    Remove-Item -Force -LiteralPath $BuiltExe
    Copy-Item -Force -LiteralPath (Join-Path $ProjectDir "config.env") -Destination $OutputDir
    Copy-Item -Force -LiteralPath (Join-Path $ProjectDir "notify.json") -Destination $OutputDir
    Copy-Item -Force -LiteralPath (Join-Path $ProjectDir "prompt.txt") -Destination $OutputDir
    Copy-Item -Force -LiteralPath (Join-Path $ProjectDir "README.md") -Destination $OutputDir
    Write-Host "Build complete: $OutputDir"
}
finally {
    Pop-Location
    if (Test-Path -LiteralPath $BuildWorkDir) {
        Remove-Item -Recurse -Force -LiteralPath $BuildWorkDir -ErrorAction SilentlyContinue
    }
}
