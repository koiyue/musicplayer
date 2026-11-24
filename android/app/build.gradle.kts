plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
}

android {
    namespace = "com.musichub.app"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.musichub.app"
        minSdk = 24
        targetSdk = 34
        // 版本号从 1.0.3.1 起递增, 每段上限 99, 满 99 进位(如 1.0.3.99 -> 1.0.4.0);
        // versionCode 保持单调递增以便覆盖安装
        versionCode = 9
        versionName = "1.0.3.2"
        // Chaquopy 需要为每个 ABI 打包 libpython; 只保留常见架构以减小体积
        ndk {
            abiFilters += listOf("arm64-v8a", "armeabi-v7a", "x86_64")
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    // 产物命名为 MusicHub-<variant>.apk, 而不是 Gradle 默认的 app-debug.apk
    applicationVariants.all {
        val variantName = name
        outputs.all {
            (this as com.android.build.gradle.internal.api.BaseVariantOutputImpl)
                .outputFileName = "MusicHub-$variantName.apk"
        }
    }
}

// 构建期把仓库根目录唯一的 sources/ 与 web/ 暂存到 build 目录再交给 Chaquopy,
// android 工程内不再保存副本(改共享代码只需改根目录那一份)。
val stagedPython = layout.buildDirectory.dir("generated/python-shared")

val stageSharedPython = tasks.register<Sync>("stageSharedPython") {
    from(rootProject.file("../sources")) { into("sources") }
    from(rootProject.file("../web")) { into("web") }
    from(rootProject.file("../web-android")) { into("web-android") }
    into(stagedPython)
}

tasks.named("preBuild") { dependsOn(stageSharedPython) }
tasks.matching { it.name.contains("Python") && !it.name.startsWith("stage") }
    .configureEach { dependsOn(stageSharedPython) }

chaquopy {
    defaultConfig {
        // 与 sources/ 代码兼容的 CPython 版本(audioop 等在 3.11 可用)
        version = "3.11"
    }
    sourceSets {
        getByName("main") {
            srcDir(stagedPython.get().asFile)
        }
    }
}
