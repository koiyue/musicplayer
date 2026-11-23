# 构建 MusicHub Android APK.
# 需要: JDK 17 + Android SDK(Platform 34 / Build-Tools 34.0.0) + 可联网.
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $here

function Find-Java {
    if ($env:JAVA_HOME -and (Test-Path "$env:JAVA_HOME\bin\java.exe")) {
        return "$env:JAVA_HOME\bin\java.exe"
    }
    $cmd = Get-Command java.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

$java = Find-Java
if (-not $java) {
    Write-Host "未找到 JDK 17。请安装 JDK 17 并设置 JAVA_HOME 后重试。" -ForegroundColor Yellow
    Write-Host "或直接用 Android Studio 打开 $here 构建。" -ForegroundColor Yellow
    exit 1
}

$sdk = $env:ANDROID_HOME
if (-not $sdk) { $sdk = $env:ANDROID_SDK_ROOT }
if (-not $sdk) {
    $guess = Join-Path $env:LOCALAPPDATA "Android\Sdk"
    if (Test-Path $guess) { $sdk = $guess }
}
if (-not $sdk -or -not (Test-Path $sdk)) {
    Write-Host "未找到 Android SDK。请安装 SDK(Platform 34/Build-Tools 34.0.0)并设置 ANDROID_HOME。" -ForegroundColor Yellow
    exit 1
}

$localProps = Join-Path $here "local.properties"
"sdk.dir=$($sdk -replace '\\','\\')" | Set-Content -Path $localProps -Encoding ASCII

Write-Host "同步 sources/ 与 web/ ..." -ForegroundColor Cyan
$py = Get-Command python.exe -ErrorAction SilentlyContinue
if (-not $py) { $py = Get-Command python -ErrorAction SilentlyContinue }
if (-not $py) {
    Write-Host "未找到 python，可手动运行: python android/sync_python.py" -ForegroundColor Yellow
} else {
    & $py.Source (Join-Path $here "sync_python.py")
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

$gradlew = Join-Path $here "gradlew.bat"
if (-not (Test-Path $gradlew)) {
    Write-Host "缺少 gradlew.bat。请先执行一次 'gradle wrapper' 或直接用 Android Studio 构建。" -ForegroundColor Yellow
    exit 1
}

Push-Location $here
try {
    & $gradlew "assembleDebug"
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} finally {
    Pop-Location
}

$apk = Join-Path $here "app\build\outputs\apk\debug\app-debug.apk"
if (Test-Path $apk) {
    Write-Host "构建成功: $apk" -ForegroundColor Green
} else {
    Write-Host "构建结束，但未找到 APK，请检查上面的输出。" -ForegroundColor Yellow
}
