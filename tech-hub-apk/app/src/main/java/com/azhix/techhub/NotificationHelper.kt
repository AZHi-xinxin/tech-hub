package com.azhix.techhub

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.graphics.BitmapFactory

object NotificationHelper {
    const val SERVICE_NOTIFICATION_ID = 7101
    const val UNREAD_NOTIFICATION_ID = 7102
    private const val SERVICE_CHANNEL = "tech_hub_polling"
    private const val UNREAD_CHANNEL = "tech_hub_unread"

    fun ensureChannels(context: Context) {
        val manager = context.getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(
            NotificationChannel(
                SERVICE_CHANNEL,
                context.getString(R.string.polling_channel_name),
                NotificationManager.IMPORTANCE_LOW,
            ).apply {
                description = "维持 1–3 分钟的本地消息检查"
                setShowBadge(false)
            },
        )
        manager.createNotificationChannel(
            NotificationChannel(
                UNREAD_CHANNEL,
                context.getString(R.string.unread_channel_name),
                NotificationManager.IMPORTANCE_HIGH,
            ).apply {
                description = "AI 或 Rikka 在 tech-hub 中发来的新消息"
                enableVibration(true)
                setShowBadge(true)
            },
        )
    }

    fun serviceNotification(context: Context, detail: String): Notification =
        Notification.Builder(context, SERVICE_CHANNEL)
            .setSmallIcon(R.drawable.ic_stat_tech_hub)
            .setContentTitle("tech-hub 门铃运行中")
            .setContentText(detail)
            .setContentIntent(openAppIntent(context))
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setCategory(Notification.CATEGORY_SERVICE)
            .build()

    fun showUnread(context: Context, messages: List<HubMessage>, totalUnread: Int) {
        if (messages.isEmpty()) return
        ensureChannels(context)
        val unique = messages.distinctBy { Triple(it.from, it.text, it.createdAt) }
        val latest = unique.last()
        val style = Notification.InboxStyle()
            .setBigContentTitle("tech-hub · ${unique.size} 条新消息")
        unique.takeLast(5).forEach {
            style.addLine("${displayName(it.from)}：${it.text.trim().take(120)}")
        }
        val notification = Notification.Builder(context, UNREAD_CHANNEL)
            .setSmallIcon(R.drawable.ic_stat_tech_hub)
            .setLargeIcon(BitmapFactory.decodeResource(context.resources, R.mipmap.ic_launcher))
            .setContentTitle("tech-hub · ${unique.size} 条新消息")
            .setContentText("${displayName(latest.from)}：${latest.text.trim().take(80)}")
            .setStyle(style)
            .setContentIntent(openAppIntent(context, openChat = true))
            .setAutoCancel(true)
            .setCategory(Notification.CATEGORY_MESSAGE)
            .setVisibility(Notification.VISIBILITY_PRIVATE)
            .setNumber(totalUnread)
            .build()
        context.getSystemService(NotificationManager::class.java)
            .notify(UNREAD_NOTIFICATION_ID, notification)
    }

    fun clearUnread(context: Context) {
        HubConfig.clearUnread(context)
        context.getSystemService(NotificationManager::class.java).cancel(UNREAD_NOTIFICATION_ID)
    }

    private fun openAppIntent(context: Context, openChat: Boolean = false): PendingIntent {
        val intent = Intent(context, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP
            putExtra(MainActivity.EXTRA_OPEN_CHAT, openChat)
        }
        return PendingIntent.getActivity(
            context,
            if (openChat) 2 else 1,
            intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
    }

    private fun displayName(identity: String): String = when (identity.lowercase()) {
        "rikka" -> "Rikka 阿止"
        "claude" -> "VS Claude"
        "codex" -> "Codex"
        "dsh" -> "DSH"
        "human" -> "Human"
        else -> identity
    }
}

