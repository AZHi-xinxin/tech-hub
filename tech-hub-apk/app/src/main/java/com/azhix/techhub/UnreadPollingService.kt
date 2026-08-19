package com.azhix.techhub

import android.app.Service
import android.content.Intent
import android.os.IBinder
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledFuture
import java.util.concurrent.TimeUnit

class UnreadPollingService : Service() {
    private val executor = Executors.newSingleThreadScheduledExecutor()
    private var schedule: ScheduledFuture<*>? = null

    override fun onCreate() {
        super.onCreate()
        NotificationHelper.ensureChannels(this)
        startForeground(
            NotificationHelper.SERVICE_NOTIFICATION_ID,
            NotificationHelper.serviceNotification(this, "正在准备检查…"),
        )
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        schedule?.cancel(false)
        val interval = HubConfig.intervalMinutes(this)
        schedule = executor.scheduleWithFixedDelay(
            { pollSafely() },
            0L,
            interval,
            TimeUnit.MINUTES,
        )
        return START_STICKY
    }

    override fun onDestroy() {
        schedule?.cancel(true)
        executor.shutdownNow()
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun pollSafely() {
        if (!HubConfig.isReady(this)) {
            updateServiceNotification("等待填写 Hub 地址与 Token")
            return
        }
        val token = HubConfig.token(this) ?: return
        val candidates = HubConfig.candidateUrls(this)
        for (baseUrl in candidates) {
            try {
                if (!HubConfig.hasCursor(this)) {
                    val baseline = HubApi.latestRoomSeq(baseUrl, token)
                    HubConfig.setCursor(this, baseline)
                    HubConfig.setActiveBaseUrl(this, baseUrl)
                    updateServiceNotification("已连接${HubConfig.profileLabel(this, baseUrl)}地址，等待新消息")
                    return
                }

                var cursor = HubConfig.cursor(this)
                val collected = ArrayList<HubMessage>()
                for (pageIndex in 0 until 5) {
                    val page = HubApi.pollMessages(baseUrl, token, cursor)
                    if (page.nextCursor > cursor) cursor = page.nextCursor
                    collected += page.events
                    if (page.events.size < 100) break
                }
                HubConfig.setCursor(this, cursor)
                HubConfig.setActiveBaseUrl(this, baseUrl)

                val unread = collected
                    .filter { it.from.lowercase() != "human" }
                    .distinctBy { it.from to it.text }
                if (unread.isNotEmpty()) {
                    val total = HubConfig.addUnread(this, unread.size)
                    NotificationHelper.showUnread(this, unread, total)
                }
                updateServiceNotification(
                    "${HubConfig.profileLabel(this, baseUrl)}地址正常 · 每 ${HubConfig.intervalMinutes(this)} 分钟",
                )
                return
            } catch (_: Exception) {
                // 自动模式继续尝试下一张地址档案；固定模式只有一个候选地址。
            }
        }
        updateServiceNotification("两个地址暂时都不可用，将自动重试")
    }

    private fun updateServiceNotification(detail: String) {
        getSystemService(android.app.NotificationManager::class.java).notify(
            NotificationHelper.SERVICE_NOTIFICATION_ID,
            NotificationHelper.serviceNotification(this, detail),
        )
    }

    companion object {
        fun start(context: android.content.Context) {
            val intent = Intent(context, UnreadPollingService::class.java)
            context.startForegroundService(intent)
        }
    }
}


