package com.azhix.techhub

import android.content.Context
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import java.net.URI
import java.security.MessageDigest

enum class AddressMode {
    AUTO,
    HOME,
    AWAY,
}

object HubConfig {
    private const val PREFS = "tech_hub_config"
    private const val KEY_LEGACY_BASE_URL = "base_url"
    private const val KEY_HOME_URL = "home_url"
    private const val KEY_AWAY_URL = "away_url"
    private const val KEY_ACTIVE_URL = "active_url"
    private const val KEY_ADDRESS_MODE = "address_mode"
    private const val KEY_CURSOR = "shared_message_cursor"
    private const val KEY_CURSOR_READY = "shared_cursor_ready"
    private const val KEY_INTERVAL = "interval_minutes"
    private const val KEY_START_ON_BOOT = "start_on_boot"
    private const val KEY_UNREAD_COUNT = "unread_count"

    fun homeUrl(context: Context): String {
        val saved = prefs(context).getString(KEY_HOME_URL, "").orEmpty()
        if (saved.isNotBlank()) return saved
        return prefs(context).getString(KEY_LEGACY_BASE_URL, "").orEmpty()
    }

    fun awayUrl(context: Context): String =
        prefs(context).getString(KEY_AWAY_URL, "").orEmpty()

    fun addressMode(context: Context): AddressMode = runCatching {
        AddressMode.valueOf(prefs(context).getString(KEY_ADDRESS_MODE, AddressMode.AUTO.name).orEmpty())
    }.getOrDefault(AddressMode.AUTO)

    fun activeBaseUrl(context: Context): String {
        val active = prefs(context).getString(KEY_ACTIVE_URL, "").orEmpty()
        val known = listOf(homeUrl(context), awayUrl(context)).filter { it.isNotBlank() }
        return active.takeIf { it in known } ?: candidateUrls(context).firstOrNull().orEmpty()
    }

    fun candidateUrls(context: Context): List<String> {
        val home = homeUrl(context)
        val away = awayUrl(context)
        return when (addressMode(context)) {
            AddressMode.HOME -> listOfNotNull(home.takeIf { it.isNotBlank() })
            AddressMode.AWAY -> listOfNotNull(away.takeIf { it.isNotBlank() })
            AddressMode.AUTO -> {
                val preferred = if (hasWifiTransport(context)) listOf(home, away) else listOf(away, home)
                val active = prefs(context).getString(KEY_ACTIVE_URL, "").orEmpty()
                (preferred + active).filter { it.isNotBlank() }.distinct()
            }
        }
    }

    fun profileLabel(context: Context, url: String = activeBaseUrl(context)): String = when (url) {
        homeUrl(context) -> "家中"
        awayUrl(context) -> "外出"
        else -> "Hub"
    }

    fun setActiveBaseUrl(context: Context, url: String) {
        if (url in listOf(homeUrl(context), awayUrl(context))) {
            prefs(context).edit().putString(KEY_ACTIVE_URL, url).apply()
        }
    }

    fun hasProfile(context: Context, mode: AddressMode): Boolean = when (mode) {
        AddressMode.AUTO -> homeUrl(context).isNotBlank() || awayUrl(context).isNotBlank()
        AddressMode.HOME -> homeUrl(context).isNotBlank()
        AddressMode.AWAY -> awayUrl(context).isNotBlank()
    }

    fun setAddressMode(context: Context, mode: AddressMode): Boolean {
        if (!hasProfile(context, mode)) return false
        prefs(context).edit().putString(KEY_ADDRESS_MODE, mode.name).apply()
        return true
    }

    fun intervalMinutes(context: Context): Long =
        prefs(context).getLong(KEY_INTERVAL, 2L).coerceIn(1L, 3L)

    fun startOnBoot(context: Context): Boolean =
        prefs(context).getBoolean(KEY_START_ON_BOOT, true)

    fun token(context: Context): String? = SecureTokenStore.get(context)

    fun isReady(context: Context): Boolean =
        candidateUrls(context).isNotEmpty() && !token(context).isNullOrBlank()

    fun saveProfiles(
        context: Context,
        rawHomeUrl: String,
        rawAwayUrl: String,
        mode: AddressMode,
        token: String,
        intervalMinutes: Long,
        startOnBoot: Boolean,
    ): Boolean {
        val home = normalizeOptionalBaseUrl(rawHomeUrl)
        val away = normalizeOptionalBaseUrl(rawAwayUrl)
        require(home.isNotBlank() || away.isNotBlank()) { "家中地址和外出地址至少填写一个" }
        if (mode == AddressMode.HOME) require(home.isNotBlank()) { "固定家中模式需要填写家中地址" }
        if (mode == AddressMode.AWAY) require(away.isNotBlank()) { "固定外出模式需要填写外出地址" }

        val oldProfiles = setOf(homeUrl(context), awayUrl(context)).filter { it.isNotBlank() }.toSet()
        val newProfiles = setOf(home, away).filter { it.isNotBlank() }.toSet()
        val active = activeBaseUrl(context).takeIf { it in newProfiles }.orEmpty()
        prefs(context).edit()
            .putString(KEY_HOME_URL, home)
            .putString(KEY_AWAY_URL, away)
            .putString(KEY_ADDRESS_MODE, mode.name)
            .putString(KEY_ACTIVE_URL, active)
            .remove(KEY_LEGACY_BASE_URL)
            .putLong(KEY_INTERVAL, intervalMinutes.coerceIn(1L, 3L))
            .putBoolean(KEY_START_ON_BOOT, startOnBoot)
            .apply()
        SecureTokenStore.put(context, token)
        return oldProfiles != newProfiles
    }

    fun normalizeBaseUrl(raw: String): String {
        var value = raw.trim().trimEnd('/')
        if (value.endsWith("/ui", ignoreCase = true)) value = value.dropLast(3)
        val uri = URI(value)
        require(uri.scheme.equals("http", true) || uri.scheme.equals("https", true)) {
            "地址必须以 http:// 或 https:// 开头"
        }
        require(!uri.host.isNullOrBlank()) { "Hub 地址缺少主机名或 IP" }
        require(uri.rawUserInfo == null) { "Hub 地址不能包含用户名或密码" }
        return value
    }

    fun hasCursor(context: Context): Boolean {
        migrateLegacyCursor(context)
        return prefs(context).getBoolean(KEY_CURSOR_READY, false)
    }

    fun cursor(context: Context): Long {
        migrateLegacyCursor(context)
        return prefs(context).getLong(KEY_CURSOR, 0L)
    }

    fun setCursor(context: Context, seq: Long) {
        prefs(context).edit()
            .putLong(KEY_CURSOR, seq.coerceAtLeast(0L))
            .putBoolean(KEY_CURSOR_READY, true)
            .apply()
    }

    fun unreadCount(context: Context): Int =
        prefs(context).getInt(KEY_UNREAD_COUNT, 0).coerceAtLeast(0)

    fun addUnread(context: Context, amount: Int): Int {
        val next = (unreadCount(context) + amount).coerceAtMost(999)
        prefs(context).edit().putInt(KEY_UNREAD_COUNT, next).apply()
        return next
    }

    fun clearUnread(context: Context) {
        prefs(context).edit().putInt(KEY_UNREAD_COUNT, 0).apply()
    }

    private fun normalizeOptionalBaseUrl(raw: String): String =
        raw.trim().takeIf { it.isNotBlank() }?.let(::normalizeBaseUrl).orEmpty()

    private fun hasWifiTransport(context: Context): Boolean {
        val manager = context.getSystemService(ConnectivityManager::class.java)
        return manager.allNetworks.any { network ->
            manager.getNetworkCapabilities(network)?.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) == true
        }
    }

    private fun migrateLegacyCursor(context: Context) {
        val p = prefs(context)
        if (p.contains(KEY_CURSOR) || p.getBoolean(KEY_CURSOR_READY, false)) return
        val legacy = listOf(homeUrl(context), awayUrl(context))
            .filter { it.isNotBlank() }
            .map { p.getLong(legacyCursorKey(it), -1L) }
            .filter { it >= 0L }
            .maxOrNull()
        if (legacy != null) {
            p.edit().putLong(KEY_CURSOR, legacy).putBoolean(KEY_CURSOR_READY, true).apply()
        }
    }

    private fun legacyCursorKey(baseUrl: String): String {
        val digest = MessageDigest.getInstance("SHA-256")
            .digest(baseUrl.toByteArray(Charsets.UTF_8))
            .joinToString("") { "%02x".format(it) }
        return "cursor_${digest.take(16)}"
    }

    private fun prefs(context: Context) =
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
}

