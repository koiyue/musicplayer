package com.musichub.app

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.media.MediaMetadata
import android.media.session.MediaSession
import android.media.session.PlaybackState
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.webkit.CookieManager
import android.webkit.JavascriptInterface
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform

/**
 * MusicHub Android 外壳: 在本地启动 Python 后端(127.0.0.1), 再用 WebView 加载前端页面.
 *
 * 桌面版专属能力(系统媒体卡片/托盘/桌面歌词/原生登录窗口)在移动端不可用,
 * 前端已有 window.pywebview 判空, 会自动降级.
 */
class MainActivity : Activity() {

    private lateinit var root: FrameLayout
    private lateinit var webView: WebView
    private var customView: View? = null
    private var customViewCallback: WebChromeClient.CustomViewCallback? = null
    private var fileCallback: ValueCallback<Array<Uri>>? = null
    // 前端能否返回(有浮层/非首页); 由 JS 通过 MHNative 桥更新
    @Volatile
    private var backAvailable = false

    // 系统媒体通知 / 锁屏播放信息
    private var mediaSession: MediaSession? = null
    private var mediaTitle = ""
    private var mediaArtist = ""

    // 网页登录浮层(账号密码), 复用系统 WebView 的 CookieManager
    private var loginOverlay: LinearLayout? = null
    private var loginWebView: WebView? = null
    private var loginSource: String = ""
    private var loginSaving = false
    private var loginPoll: Runnable? = null
    private val uiHandler = Handler(Looper.getMainLooper())

    /** 给前端用的最小桥接: 返回状态上报 + 打开网页登录 */
    inner class JsBridge {
        @JavascriptInterface
        fun setBackAvailable(available: Boolean) {
            backAvailable = available
        }

        @JavascriptInterface
        fun openWebLogin(source: String) {
            runOnUiThread { this@MainActivity.openWebLogin(source) }
        }

        /** 音译: 汉字/假名/谚文转拉丁转写(拼音/罗马音), 并去掉声调符号. */
        @JavascriptInterface
        fun transliterate(text: String): String {
            if (text.isBlank()) return ""
            val t = latinTransliterator ?: return ""
            return try {
                t.transliterate(text)
            } catch (_: Throwable) {
                ""
            }
        }

        /** 更新系统通知/锁屏上的播放信息(前端在播放、暂停、切歌、进度变化时调用). */
        @JavascriptInterface
        fun updateMedia(title: String, artist: String, playing: Boolean,
                        positionMs: Double, durationMs: Double) {
            runOnUiThread {
                mediaTitle = title
                mediaArtist = artist
                publishMedia(playing, positionMs.toLong(), durationMs.toLong())
            }
        }
    }

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        root = FrameLayout(this)
        webView = WebView(this)
        root.addView(
            webView,
            FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT
            )
        )
        setContentView(root)

        webView.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            databaseEnabled = true
            mediaPlaybackRequiresUserGesture = false
            cacheMode = WebSettings.LOAD_DEFAULT
            useWideViewPort = true
            loadWithOverviewMode = true
            setSupportZoom(false)
            mixedContentMode = WebSettings.MIXED_CONTENT_NEVER_ALLOW
        }

        webView.addJavascriptInterface(JsBridge(), "MHNative")

        setupMediaSession()
        // Android 13+ 通知需要运行时授权
        if (Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) !=
            PackageManager.PERMISSION_GRANTED
        ) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), REQ_NOTIFY)
        }

        webView.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(
                view: WebView,
                request: WebResourceRequest
            ): Boolean {
                val host = request.url.host ?: return false
                if (host == "127.0.0.1" || host == "localhost") return false
                return try {
                    startActivity(Intent(Intent.ACTION_VIEW, request.url))
                    true
                } catch (_: Exception) {
                    true
                }
            }
        }

        webView.webChromeClient = object : WebChromeClient() {
            // 支持 <input type="file"> (皮肤上传等)
            override fun onShowFileChooser(
                view: WebView,
                callback: ValueCallback<Array<Uri>>,
                params: FileChooserParams
            ): Boolean {
                fileCallback?.onReceiveValue(null)
                fileCallback = callback
                return try {
                    startActivityForResult(params.createIntent(), REQ_FILE)
                    true
                } catch (_: Exception) {
                    fileCallback = null
                    false
                }
            }

            // 支持网页全屏(视频)
            override fun onShowCustomView(view: View, callback: CustomViewCallback) {
                if (customView != null) {
                    callback.onCustomViewHidden()
                    return
                }
                customView = view
                customViewCallback = callback
                root.addView(
                    view,
                    FrameLayout.LayoutParams(
                        ViewGroup.LayoutParams.MATCH_PARENT,
                        ViewGroup.LayoutParams.MATCH_PARENT
                    )
                )
                webView.visibility = View.GONE
            }

            override fun onHideCustomView() {
                val v = customView ?: return
                root.removeView(v)
                customView = null
                webView.visibility = View.VISIBLE
                customViewCallback?.onCustomViewHidden()
                customViewCallback = null
            }
        }

        // Chaquopy 必须先绑定 Android 平台, 否则 getInstance() 会报 GenericPlatform
        if (!Python.isStarted()) {
            Python.start(AndroidPlatform(applicationContext))
        }

        // 后端起在后台线程: 首次启动要解压 Python 运行时, 不能阻塞主线程
        Thread {
            try {
                val port = Python.getInstance()
                    .getModule("android_entry")
                    .callAttr("start", "$filesDir/musichub")
                    .toInt()
                // 把安装包版本带给前端, 用于核对当前运行的是哪一版
                val version = try {
                    packageManager.getPackageInfo(packageName, 0).versionName ?: ""
                } catch (_: Exception) {
                    ""
                }
                runOnUiThread {
                    webView.loadUrl("http://127.0.0.1:$port/?v=" + Uri.encode(version))
                }
            } catch (e: Throwable) {
                runOnUiThread {
                    Toast.makeText(this, "后端启动失败: ${e.message}", Toast.LENGTH_LONG).show()
                }
            }
        }.start()
    }

    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        if (requestCode == REQ_FILE) {
            var uris: Array<Uri>? = null
            if (resultCode == RESULT_OK && data != null) {
                val clip = data.clipData
                uris = if (clip != null) {
                    Array(clip.itemCount) { i -> clip.getItemAt(i).uri }
                } else {
                    data.data?.let { arrayOf(it) }
                }
            }
            fileCallback?.onReceiveValue(uris)
            fileCallback = null
            return
        }
        super.onActivityResult(requestCode, resultCode, data)
    }

    // ---------- 网页登录(账号密码) ----------

    private fun dp(v: Int): Int = (v * resources.displayMetrics.density).toInt()

    private fun openWebLogin(source: String) {
        val cfg = WEB_LOGIN[source] ?: return
        if (loginOverlay != null) closeWebLogin()
        loginSource = source
        loginSaving = false

        val overlay = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        val bar = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(dp(12), dp(8), dp(8), dp(8))
            setBackgroundColor(Color.parseColor("#12121A"))
        }
        val title = TextView(this).apply {
            text = cfg.title
            setTextColor(Color.WHITE)
            textSize = 15f
            maxLines = 1
        }
        bar.addView(title, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        val cancel = Button(this).apply {
            text = "取消"
            setTextColor(Color.parseColor("#B9B9C8"))
        }
        val done = Button(this).apply {
            text = "完成"
            setTextColor(Color.parseColor("#7C8CFF"))
        }
        bar.addView(cancel)
        bar.addView(done)

        val wv = WebView(this)
        wv.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            databaseEnabled = true
            mediaPlaybackRequiresUserGesture = false
            useWideViewPort = true
            loadWithOverviewMode = true
            setSupportZoom(true)
            builtInZoomControls = true
            displayZoomControls = false
            if (cfg.desktopUa) userAgentString = DESKTOP_UA
        }
        CookieManager.getInstance().setAcceptCookie(true)
        CookieManager.getInstance().setAcceptThirdPartyCookies(wv, true)

        wv.webViewClient = object : WebViewClient() {
            override fun onPageFinished(view: WebView, url: String?) {
                maybeSaveWebLogin()
            }
        }
        wv.webChromeClient = object : WebChromeClient() {
            override fun onProgressChanged(view: WebView, newProgress: Int) {
                if (newProgress >= 85) maybeSaveWebLogin()
            }
        }

        overlay.addView(bar, LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT))
        overlay.addView(wv, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f))
        root.addView(overlay, FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
        loginOverlay = overlay
        loginWebView = wv
        webView.visibility = View.GONE

        cancel.setOnClickListener { closeWebLogin() }
        done.setOnClickListener {
            if (!saveWebLogin()) {
                Toast.makeText(this, "未检测到有效登录态，请先在页面完成登录", Toast.LENGTH_LONG).show()
            }
        }

        wv.loadUrl(cfg.url)
        // 登录成功通常只体现在 Cookie 上, 定时轮询自动收口
        loginPoll = object : Runnable {
            override fun run() {
                if (loginOverlay == null) return
                maybeSaveWebLogin()
                uiHandler.postDelayed(this, 1200)
            }
        }.also { uiHandler.postDelayed(it, 1500) }
    }

    private fun maybeSaveWebLogin() {
        if (loginSaving || loginOverlay == null) return
        val ck = collectCookies()
        if (ck.isEmpty() || !cookiesReady(ck)) return
        saveWebLogin()
    }

    /** 收集登录页涉及的站点 Cookie; 必需的登录键齐全时写入 Python 侧. */
    private fun saveWebLogin(): Boolean {
        if (loginSaving) return true
        val ck = collectCookies()
        if (ck.isEmpty() || !cookiesReady(ck)) return false
        loginSaving = true
        val source = loginSource
        Toast.makeText(this, "已获取登录信息，正在保存…", Toast.LENGTH_SHORT).show()
        Thread {
            val ok = try {
                Python.getInstance().getModule("android_entry")
                    .callAttr("save_cookie", source, ck)
                    .toBoolean()
            } catch (e: Throwable) {
                false
            }
            runOnUiThread {
                loginSaving = false
                if (ok) {
                    Toast.makeText(this, "登录成功", Toast.LENGTH_SHORT).show()
                    closeWebLogin()
                    webView.evaluateJavascript("window.mhLoginDone && window.mhLoginDone()", null)
                } else {
                    Toast.makeText(this, "登录信息不完整，请确认已在页面登录成功", Toast.LENGTH_LONG).show()
                }
            }
        }.start()
        return true
    }

    private fun collectCookies(): String {
        val cfg = WEB_LOGIN[loginSource] ?: return ""
        val cm = CookieManager.getInstance()
        val set = LinkedHashSet<String>()
        for (u in cfg.cookieUrls) {
            val raw = cm.getCookie(u) ?: continue
            for (seg in raw.split(";")) {
                val s = seg.trim()
                if (s.contains("=")) set.add(s)
            }
        }
        return set.joinToString("; ")
    }

    private fun cookiesReady(ck: String): Boolean {
        fun has(name: String) =
            Regex("(?:^|;\\s*)" + Regex.escape(name) + "=").containsMatchIn(ck)

        return when (loginSource) {
            "netease" -> has("MUSIC_U")
            "bilibili" -> has("SESSDATA") && has("bili_jct")
            "qq" -> has("uin") && listOf("qqmusic_key", "qm_keyst", "music_key",
                "skey", "qm36").any { has(it) }
            "douyin" -> has("sessionid") || has("sid_tt")
            else -> false
        }
    }

    private fun closeWebLogin() {
        loginPoll?.let { uiHandler.removeCallbacks(it) }
        loginPoll = null
        val wv = loginWebView
        val overlay = loginOverlay
        loginWebView = null
        loginOverlay = null
        if (wv != null) {
            try {
                wv.stopLoading()
                (wv.parent as? ViewGroup)?.removeView(wv)
                wv.destroy()
            } catch (_: Exception) {
            }
        }
        if (overlay != null) {
            try {
                root.removeView(overlay)
            } catch (_: Exception) {
            }
        }
        webView.visibility = View.VISIBLE
    }

    // ---------- 系统媒体通知 / 锁屏控制 ----------

    /** 在 WebView 中执行一段 JS(主线程) */
    private fun js(code: String) {
        runOnUiThread {
            try { webView.evaluateJavascript(code, null) } catch (_: Throwable) {}
        }
    }

    private fun setupMediaSession() {
        val s = MediaSession(this, "MusicHub")
        s.setCallback(object : MediaSession.Callback() {
            override fun onPlay() = js("window.mhPlay && window.mhPlay()")
            override fun onPause() = js("window.mhPause && window.mhPause()")
            override fun onSkipToNext() = js("window.mhNext && window.mhNext()")
            override fun onSkipToPrevious() = js("window.mhPrev && window.mhPrev()")
            override fun onSeekTo(pos: Long) =
                js("window.mhSeekAt && window.mhSeekAt(${pos / 1000.0})")
        })
        s.isActive = true
        mediaSession = s
    }

    private fun actionIntent(action: String, code: Int): PendingIntent {
        val i = Intent(this, MainActivity::class.java).setAction(action)
        var flags = PendingIntent.FLAG_UPDATE_CURRENT
        if (Build.VERSION.SDK_INT >= 23) flags = flags or PendingIntent.FLAG_IMMUTABLE
        return PendingIntent.getActivity(this, code, i, flags)
    }

    private fun canNotify(): Boolean =
        Build.VERSION.SDK_INT < 33 ||
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) ==
            PackageManager.PERMISSION_GRANTED

    private fun ensureChannel(nm: NotificationManager) {
        if (Build.VERSION.SDK_INT >= 26) {
            val ch = NotificationChannel(CHANNEL_ID, "播放状态", NotificationManager.IMPORTANCE_LOW)
            ch.setShowBadge(false)
            nm.createNotificationChannel(ch)
        }
    }

    /** 用 MediaSession + MediaStyle 通知展示当前歌曲, 并接管通知栏/锁屏的播放控制 */
    private fun publishMedia(playing: Boolean, positionMs: Long, durationMs: Long) {
        if (!canNotify()) return
        val s = mediaSession ?: return
        val title = mediaTitle.ifBlank { "未在播放" }
        val artist = mediaArtist.ifBlank { "MusicHub" }
        s.setMetadata(MediaMetadata.Builder()
            .putString(MediaMetadata.METADATA_KEY_TITLE, title)
            .putString(MediaMetadata.METADATA_KEY_ARTIST, artist)
            .putString(MediaMetadata.METADATA_KEY_ALBUM, "MusicHub")
            .putLong(MediaMetadata.METADATA_KEY_DURATION, durationMs)
            .build())
        val actions = PlaybackState.ACTION_PLAY or PlaybackState.ACTION_PAUSE or
            PlaybackState.ACTION_PLAY_PAUSE or PlaybackState.ACTION_SKIP_TO_NEXT or
            PlaybackState.ACTION_SKIP_TO_PREVIOUS or PlaybackState.ACTION_SEEK_TO
        s.setPlaybackState(PlaybackState.Builder()
            .setActions(actions)
            .setState(
                if (playing) PlaybackState.STATE_PLAYING else PlaybackState.STATE_PAUSED,
                positionMs, if (playing) 1f else 0f)
            .build())

        val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        ensureChannel(nm)
        val toggleIcon = if (playing) android.R.drawable.ic_media_pause
        else android.R.drawable.ic_media_play
        @Suppress("DEPRECATION")
        val nb = if (Build.VERSION.SDK_INT >= 26) Notification.Builder(this, CHANNEL_ID)
        else Notification.Builder(this)
        val b = nb
            .setSmallIcon(android.R.drawable.ic_media_play)
            .setContentTitle(title)
            .setContentText(artist)
            .setContentIntent(actionIntent(ACT_OPEN, 1))
            .setOngoing(playing)
            .setOnlyAlertOnce(true)
            .setShowWhen(false)
            .setVisibility(Notification.VISIBILITY_PUBLIC)
            .addAction(Notification.Action.Builder(
                android.R.drawable.ic_media_previous, "上一首", actionIntent(ACT_PREV, 2)).build())
            .addAction(Notification.Action.Builder(
                toggleIcon, if (playing) "暂停" else "播放", actionIntent(ACT_TOGGLE, 3)).build())
            .addAction(Notification.Action.Builder(
                android.R.drawable.ic_media_next, "下一首", actionIntent(ACT_NEXT, 4)).build())
            .setStyle(Notification.MediaStyle()
                .setMediaSession(s.sessionToken)
                .setShowActionsInCompactView(0, 1, 2))
        try { nm.notify(NOTIFY_ID, b.build()) } catch (_: Throwable) {}
    }

    override fun onNewIntent(intent: Intent?) {
        super.onNewIntent(intent)
        when (intent?.action) {
            ACT_PREV -> js("window.mhPrev && window.mhPrev()")
            ACT_NEXT -> js("window.mhNext && window.mhNext()")
            ACT_TOGGLE -> js("window.mhTogglePlay && window.mhTogglePlay()")
            else -> {}
        }
    }

    @Deprecated("Deprecated in Java")
    override fun onBackPressed() {
        if (customView != null) {
            webView.webChromeClient?.onHideCustomView()
            return
        }
        if (loginOverlay != null) {
            closeWebLogin()
            return
        }
        // 返回键与全面屏侧滑返回统一交给前端处理: 先关浮层/回首页, 无处可返回时才退出
        if (backAvailable) {
            webView.evaluateJavascript("window.mhBack && window.mhBack()", null)
            return
        }
        super.onBackPressed()
    }

    override fun onDestroy() {
        closeWebLogin()
        try {
            (getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager)
                .cancel(NOTIFY_ID)
        } catch (_: Throwable) {}
        mediaSession?.release()
        mediaSession = null
        webView.destroy()
        super.onDestroy()
    }

    private data class WebLoginCfg(
        val title: String,
        val url: String,
        val cookieUrls: List<String>,
        val desktopUa: Boolean = false
    )

    companion object {
        private const val REQ_FILE = 1001
        private const val REQ_NOTIFY = 1002
        private const val CHANNEL_ID = "musichub_media"
        private const val NOTIFY_ID = 2001
        private const val ACT_OPEN = "com.musichub.app.OPEN"
        private const val ACT_PREV = "com.musichub.app.PREV"
        private const val ACT_NEXT = "com.musichub.app.NEXT"
        private const val ACT_TOGGLE = "com.musichub.app.TOGGLE"

        // android.icu 的 Any-Latin 覆盖汉字->拼音、假名->罗马音、谚文->罗马字;
        // Latin-ASCII 去掉拼音声调符号, 便于跟唱
        private val latinTransliterator: android.icu.text.Transliterator? by lazy {
            try {
                android.icu.text.Transliterator.getInstance("Any-Latin; Latin-ASCII")
            } catch (_: Throwable) {
                null
            }
        }
        private const val DESKTOP_UA =
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

        // 官方网页登录地址: 账号/密码流程由官网自己处理(含验证码), 登录后回收 Cookie
        private val WEB_LOGIN = mapOf(
            "netease" to WebLoginCfg(
                "网易云登录",
                "https://music.163.com/#/login",
                listOf("https://music.163.com/"),
                desktopUa = true
            ),
            "bilibili" to WebLoginCfg(
                "哔哩哔哩登录",
                "https://passport.bilibili.com/login",
                listOf("https://www.bilibili.com/", "https://passport.bilibili.com/")
            ),
            "qq" to WebLoginCfg(
                "QQ音乐登录",
                "https://y.qq.com/",
                listOf("https://y.qq.com/", "https://music.qq.com/")
            ),
            "douyin" to WebLoginCfg(
                "抖音登录",
                "https://www.douyin.com/",
                listOf("https://www.douyin.com/")
            )
        )
    }
}
