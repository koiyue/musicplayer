# MusicHub Android 版

把现有 Python 后端（`sources/`）与 Web 前端（`web/`）打包成一个独立 APK：

- **Chaquopy** 在应用进程内运行 CPython 3.11，启动 `sources/server.py` 的本地 HTTP 服务；
- **WebView** 加载 `http://127.0.0.1:<port>/`，复用桌面版同一套界面与播放逻辑；
- 服务只监听 `127.0.0.1`，并沿用服务端随机令牌，外部网络无法访问本地 API。

## 共享核心 + 两套界面

- 核心：`sources/`（唯一一份：音源、/proxy 流代理、令牌校验、数据目录）
- 桌面界面：`web/`（完整功能：歌单、歌词、弹幕、一起听、桌面歌词等）
- Android 界面：`web-android/`（移动端布局：首页卡片、搜索页、底部播放条、队列面板、音源切换）

`android_entry.py` 设置 `MUSICHUB_CLIENT=android`，`sources/server.py` 据此在根路径返回
`web-android/index.html`，静态资源走 `/m/*`；Windows 不设置该变量，仍使用 `web/`。

## 端口占用

- `serve_auto(preferred)` 直接尝试绑定：8787 → 8788 … → 8806 → 系统分配；
- Windows 下关闭 `SO_REUSEADDR`，避免“绑定成功但请求被旧进程截获”；
- Android 端拿到实际端口后再交给 WebView，因此桌面版同时开着也不会冲突。

## 目录结构

```
android/
├── app/
│   ├── build.gradle.kts          # 应用 + Chaquopy 配置
│   └── src/main/
│       ├── AndroidManifest.xml
│       ├── java/com/musichub/app/MainActivity.kt   # WebView 外壳 + Python 启动
│       ├── python/
│       │   └── android_entry.py  # 启动本地服务并返回端口
│       └── res/
├── settings.gradle.kts
├── build.gradle.kts
└── sync_python.py                # 同步共享代码
```

## 构建前提

- JDK 17
- Android SDK：Platform 34、Build-Tools 34.0.0、Platform-Tools
- 首次构建需联网：Gradle 发行版、AGP/Kotlin/Chaquopy 依赖、各 ABI 的 Python 运行时

推荐直接用 **Android Studio**（Koala 或更新版本）打开 `android/` 目录：

1. 运行 `python android/sync_python.py`（或让构建脚本代劳），把 `sources/`、`web/` 复制进工程；
2. Android Studio 打开 `android/`，等待 Gradle Sync 完成；
3. `Run ▶` 安装到设备/模拟器，或 `Build → Build App Bundle(s) / APK(s) → Build APK(s)`。

命令行构建（需自行安装 JDK/SDK，并生成 Gradle Wrapper）：

```powershell
python android/sync_python.py
cd android
# 若仓库未带 gradle-wrapper.jar，先执行一次: gradle wrapper
.\gradlew assembleDebug
# 产物: android/app/build/outputs/apk/debug/app-debug.apk
```

`local.properties` 需要指向 SDK（Android Studio 会自动生成）：

```properties
sdk.dir=C\:\\Users\\<name>\\AppData\\Local\\Android\\Sdk
```

也可以直接运行仓库内的 `build_apk.ps1`（会检查 JDK/SDK 环境并给出提示）。

## 相对桌面版的差异

| 能力 | Android |
|---|---|
| 浏览/搜索/播放/歌单/下载 | 支持（同一套后端与前端） |
| 网易云/QQ/咪咕 接口 | 支持 |
| B 站视频与弹幕 | 支持（WebView 内播放） |
| 抖音视频 | 支持 |
| 系统媒体卡片 / 托盘 / 桌面歌词 | 不支持（依赖 Windows 原生 API） |
| 原生扫码登录窗口 | 不支持（桌面版专属流程） |
| 系统音频回环 ASR | 未启用（需要 pyaudiowpatch/torch，未打包） |
| 后台/锁屏播放 | 目前跟随 Activity 生命周期，未实现前台服务 |

## 数据目录

应用启动时把 `MUSICHUB_DATA_DIR` 指向应用私有目录 `files/musichub`，队列、设置、歌单、下载与登录凭据都写在这里，卸载即清除。非 Windows 平台没有 DPAPI，凭据以明文 JSON 存在应用私有沙箱内（其他应用无法读取）。

## 共享代码

Android 工程内**不保存** `sources/`、`web/`、`web-android/` 副本。Gradle 的
`stageSharedPython` 任务会在每次构建时把它们暂存到 `app/build/generated/python-shared/`，
再交给 Chaquopy 打包。改代码只需改仓库根目录对应目录，重新构建即可生效。
