package com.musichub.app

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.media.session.MediaSession
import android.os.Build
import android.os.IBinder

/**
 * 前台服务: 承载系统媒体通知.
 *
 * 锁屏能显示而通知中心不显示, 是部分 ROM(如 MIUI/HyperOS) 对"非前台服务"媒体通知的折叠策略;
 * 统一改用 startForeground 发出的媒体通知, 系统才会把它放进通知中心的播放器卡片.
 */
class MediaPlaybackService : Service() {

    data class Payload(
        var title: String = "未在播放",
        var artist: String = "MusicHub",
        var playing: Boolean = false,
        var positionMs: Long = 0,
        var durationMs: Long = 0,
        var token: MediaSession.Token? = null,
    )

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        instance = this
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        apply(pending ?: Payload())
        return START_NOT_STICKY
    }

    override fun onDestroy() {
        instance = null
        super.onDestroy()
    }

    /** 更新/发布前台媒体通知(主线程) */
    fun apply(p: Payload) {
        if (!canNotify()) return
        val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        ensureChannel(nm)
        val n = build(p)
        try {
            if (Build.VERSION.SDK_INT >= 29) {
                startForeground(NOTIFY_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK)
            } else {
                startForeground(NOTIFY_ID, n)
            }
        } catch (_: Throwable) {
            // 极端情况下(后台限制)退回普通通知, 至少让信息可见
            try { nm.notify(NOTIFY_ID, n) } catch (_: Throwable) {}
        }
    }

    private fun canNotify(): Boolean {
        if (Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS) !=
            android.content.pm.PackageManager.PERMISSION_GRANTED
        ) return false
        val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        return nm.areNotificationsEnabled()
    }

    private fun build(p: Payload): Notification {
        @Suppress("DEPRECATION")
        val b = if (Build.VERSION.SDK_INT >= 26) Notification.Builder(this, CHANNEL_ID)
        else Notification.Builder(this)
        b.setSmallIcon(android.R.drawable.ic_media_play)
            .setContentTitle(p.title)
            .setContentText(p.artist)
            .setContentIntent(actionIntent(ACT_OPEN, 1))
            .setCategory(Notification.CATEGORY_TRANSPORT)
            .setOngoing(p.playing)
            .setOnlyAlertOnce(true)
            .setShowWhen(false)
            .setVisibility(Notification.VISIBILITY_PUBLIC)
            .addAction(Notification.Action.Builder(
                android.R.drawable.ic_media_previous, "上一首",
                actionIntent(ACT_PREV, 2)).build())
            .addAction(Notification.Action.Builder(
                if (p.playing) android.R.drawable.ic_media_pause
                else android.R.drawable.ic_media_play,
                if (p.playing) "暂停" else "播放", actionIntent(ACT_TOGGLE, 3)).build())
            .addAction(Notification.Action.Builder(
                android.R.drawable.ic_media_next, "下一首",
                actionIntent(ACT_NEXT, 4)).build())
        val token = p.token
        if (token != null) {
            b.setStyle(Notification.MediaStyle()
                .setMediaSession(token)
                .setShowActionsInCompactView(0, 1, 2))
        }
        return b.build()
    }

    private fun actionIntent(action: String, code: Int): PendingIntent {
        val i = Intent(this, MainActivity::class.java).setAction(action)
        var flags = PendingIntent.FLAG_UPDATE_CURRENT
        if (Build.VERSION.SDK_INT >= 23) flags = flags or PendingIntent.FLAG_IMMUTABLE
        return PendingIntent.getActivity(this, code, i, flags)
    }

    private fun ensureChannel(nm: NotificationManager) {
        if (Build.VERSION.SDK_INT >= 26) {
            val ch = NotificationChannel(CHANNEL_ID, "播放控制", NotificationManager.IMPORTANCE_DEFAULT)
            ch.setShowBadge(false)
            ch.setSound(null, null)
            nm.createNotificationChannel(ch)
        }
    }

    companion object {
        private const val CHANNEL_ID = "musichub_media2"
        private const val NOTIFY_ID = 2001
        private const val ACT_OPEN = "com.musichub.app.OPEN"
        private const val ACT_PREV = "com.musichub.app.PREV"
        private const val ACT_NEXT = "com.musichub.app.NEXT"
        private const val ACT_TOGGLE = "com.musichub.app.TOGGLE"

        @Volatile
        private var instance: MediaPlaybackService? = null

        @Volatile
        private var pending: Payload? = null

        /** 由 Activity 调用: 服务没起来就起前台服务, 已起来则直接刷新通知 */
        fun push(ctx: Context, p: Payload) {
            val svc = instance
            if (svc != null) {
                svc.apply(p)
                return
            }
            pending = p
            try {
                val i = Intent(ctx, MediaPlaybackService::class.java)
                if (Build.VERSION.SDK_INT >= 26) ctx.startForegroundService(i)
                else ctx.startService(i)
            } catch (_: Throwable) {
                // 后台启动受限时忽略; 下次前台播放会重新启动
            }
        }

        fun stop(ctx: Context) {
            try { instance?.stopSelf() } catch (_: Throwable) {}
            instance = null
            pending = null
            try {
                (ctx.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager)
                    .cancel(NOTIFY_ID)
            } catch (_: Throwable) {}
        }
    }
}
