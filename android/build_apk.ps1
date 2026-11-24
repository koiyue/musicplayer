# 构建 MusicHub Android APK.
# 需要: JDK 17 + Android SDK(Platform 34 / Build-Tools 34.0.0) + 可联网.
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $here

function Find-Java {
    if ($env:JAVA_HOME -and (Test-Path "$env:JAVA_HOME\bin\java.exe")) {
        return "$env:JAVA_HOME\bin\java.exe"
    }
    $tool = "$env:USERPROFILE\mh-android-tools\jdk17\bin\java.exe"
    if (Test-Path $tool) { return $tool }
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
# Gradle 依赖 JAVA_HOME
$env:JAVA_HOME = Split-Path -Parent (Split-Path -Parent $java)

$sdk = $env:ANDROID_HOME
if (-not $sdk) { $sdk = $env:ANDROID_SDK_ROOT }
if (-not $sdk) {
    $toolSdk = "$env:USERPROFILE\mh-android-tools\sdk"
    if (Test-Path $toolSdk) { $sdk = $toolSdk }
}
if (-not $sdk) {
    $guess = Join-Path $env:LOCALAPPDATA "Android\Sdk"
    if (Test-Path $guess) { $sdk = $guess }
}
if (-not $sdk -or -not (Test-Path $sdk)) {
    Write-Host "未找到 Android SDK。请安装 SDK(Platform 34/Build-Tools 34.0.0)并设置 ANDROID_HOME。" -ForegroundColor Yellow
    exit 1
}
$env:ANDROID_HOME = $sdk
$env:ANDROID_SDK_ROOT = $sdk

# 复用本机工具链已下载的 Gradle 依赖缓存, 避免重复拉取
if (-not $env:GRADLE_USER_HOME) {
    $toolCache = "$env:USERPROFILE\mh-android-tools\gradle-home"
    if (Test-Path $toolCache) { $env:GRADLE_USER_HOME = $toolCache }
}

$localProps = Join-Path $here "local.properties"
"sdk.dir=$($sdk -replace '\\','\\')" | Set-Content -Path $localProps -Encoding ASCII

# 共享代码由 Gradle 的 stageSharedPython 任务在构建期暂存, 无需手动同步

# 优先用仓库自带的 gradlew; 否则用本机工具链里的 Gradle 8.7
$gradle = $null
if (Test-Path (Join-Path $here "gradlew.bat")) {
    $gradle = Join-Path $here "gradlew.bat"
} elseif (Test-Path "$env:USERPROFILE\mh-android-tools\gradle-8.7\bin\gradle.bat") {
    $gradle = "$env:USERPROFILE\mh-android-tools\gradle-8.7\bin\gradle.bat"
} else {
    $cmd = Get-Command gradle.bat -ErrorAction SilentlyContinue
    if ($cmd) { $gradle = $cmd.Source }
}
if (-not $gradle) {
    Write-Host "未找到 Gradle。请安装 Gradle 8.7，或执行一次 'gradle wrapper'，或用 Android Studio 构建。" -ForegroundColor Yellow
    exit 1
}

Push-Location $here
try {
    if ($gradle -like "*gradlew.bat") {
        & $gradle "assembleDebug"
    } else {
        & $gradle -p $here assembleDebug
    }
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} finally {
    Pop-Location
}

$apkDir = Join-Path $here "app\build\outputs\apk\debug"
$apk = Get-ChildItem $apkDir -Filter *.apk -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($apk) {
    $dist = Join-Path $here "dist"
    New-Item -ItemType Directory -Force -Path $dist | Out-Null
    Copy-Item $apk.FullName (Join-Path $dist $apk.Name) -Force
    Write-Host "构建成功: $($apk.FullName)" -ForegroundColor Green
    Write-Host "已复制到: $(Join-Path $dist $apk.Name)" -ForegroundColor Green
} else {
    Write-Host "构建结束，但未找到 APK，请检查上面的输出。" -ForegroundColor Yellow
}
