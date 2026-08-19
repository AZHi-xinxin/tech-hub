package com.azhix.techhub

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action !in setOf(Intent.ACTION_BOOT_COMPLETED, Intent.ACTION_MY_PACKAGE_REPLACED)) return
        if (HubConfig.startOnBoot(context) && HubConfig.isReady(context)) {
            runCatching { UnreadPollingService.start(context) }
        }
    }
}

