param([string]$Version = "0.1.0")
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
if ($env:OS -ne "Windows_NT") { throw "Build on 64-bit Windows." }
if ($Version -notmatch '^\d+\.\d+\.\d+([.-][A-Za-z0-9.-]+)?$') { throw "Invalid version." }
$Root = Split-Path $PSScriptRoot -Parent
$Build = Join-Path $Root "dist/windows"
$Bundle = Join-Path $Build "Modelfiche"
$Release = Join-Path $Build "release"
$PythonVersion = "3.13.15"
$PythonSha256 = "d1f04d990aee1253d8569e8e5104e30fa9f5fa830899f14843448872d936a2cf"
function Run([string]$Command, [string[]]$Arguments) {
    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Command failed ($LASTEXITCODE)." }
}
if (Test-Path $Build) { Remove-Item $Build -Recurse -Force }
New-Item -ItemType Directory -Force $Bundle, $Release, "$Bundle/runtime" | Out-Null
Push-Location $Root
try {
    Run "pnpm" @("--dir", "apps/web", "build")
    $Archive = Join-Path $Build "python-embed.zip"
    Invoke-WebRequest "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip" -OutFile $Archive
    if ((Get-FileHash $Archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $PythonSha256) { throw "Python archive checksum mismatch." }
    Expand-Archive $Archive "$Bundle/runtime"
    @("python313.zip", ".", "Lib/site-packages", "import site") | Set-Content "$Bundle/runtime/python313._pth" -Encoding ascii
    Run "uv" @("export", "--frozen", "--no-dev", "--no-emit-project", "--output-file", "$Build/requirements.txt")
    Run "uv" @("pip", "install", "--python", "$Bundle/runtime/python.exe", "--target", "$Bundle/runtime/Lib/site-packages", "--only-binary", ":all:", "--require-hashes", "-r", "$Build/requirements.txt")
    Run "uv" @("build", "--wheel", "--out-dir", "$Build/wheels")
    $Wheel = (Get-ChildItem "$Build/wheels/*.whl").FullName
    Run "uv" @("pip", "install", "--python", "$Bundle/runtime/python.exe", "--target", "$Bundle/runtime/Lib/site-packages", "--no-deps", $Wheel)
    Copy-Item "apps/web/dist" "$Bundle/web" -Recurse
    Copy-Item "apps/api/alembic" "$Bundle/alembic" -Recurse
    Copy-Item "docs/WINDOWS.md" "$Bundle/README.md"
    # C# uses the Windows .NET Framework, with no extra desktop runtime download.
    $Csc = Join-Path $env:WINDIR "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    $Launcher = Join-Path $Root "scripts\windows\Launcher.cs"
    $Manifest = Join-Path $Root "scripts\windows\app.manifest"
    $Refs = @("/r:System.Windows.Forms.dll", "/r:System.Drawing.dll", "/r:System.Core.dll")
    Run $Csc (@("/nologo", "/target:winexe", "/platform:x64", "/out:$Bundle/Modelfiche.exe", "/win32manifest:$Manifest") + $Refs + @($Launcher))
    Run $Csc (@("/nologo", "/target:exe", "/platform:x64", "/define:CLI", "/out:$Bundle/mfiche.exe", "/win32manifest:$Manifest") + $Refs + @($Launcher))
    @{ version = $Version; platform = "windows-x64"; python = $PythonVersion; commit = (& git rev-parse HEAD) } | ConvertTo-Json | Set-Content "$Bundle/build-info.json" -Encoding utf8
    $Zip = Join-Path $Release "Modelfiche-$Version-windows-x64.zip"
    Compress-Archive -Path $Bundle -DestinationPath $Zip -CompressionLevel Optimal
    $Hash = (Get-FileHash $Zip -Algorithm SHA256).Hash.ToLowerInvariant()
    "$Hash  $([IO.Path]::GetFileName($Zip))" | Set-Content "$Release/Modelfiche-$Version-windows-SHA256SUMS.txt" -Encoding ascii
    Write-Host "Built $Zip"
} finally { Pop-Location }
