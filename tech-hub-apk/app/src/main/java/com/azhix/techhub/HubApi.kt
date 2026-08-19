package com.azhix.techhub

import org.json.JSONObject
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder

data class HubMessage(
    val seq: Long,
    val from: String,
    val text: String,
    val createdAt: String,
)

data class PollResult(
    val events: List<HubMessage>,
    val nextCursor: Long,
)

object HubApi {
    private const val CONNECT_TIMEOUT_MS = 10_000
    private const val READ_TIMEOUT_MS = 15_000

    fun latestRoomSeq(baseUrl: String, token: String, room: String = "general"): Long {
        val body = getJson("$baseUrl/rooms", token)
        val rooms = body.getJSONArray("rooms")
        for (i in 0 until rooms.length()) {
            val item = rooms.getJSONObject(i)
            if (item.optString("room") == room) return item.optLong("last_seq", 0L)
        }
        return 0L
    }

    fun pollMessages(
        baseUrl: String,
        token: String,
        after: Long,
        room: String = "general",
    ): PollResult {
        val encodedRoom = URLEncoder.encode(room, Charsets.UTF_8.name()).replace("+", "%20")
        val body = getJson(
            "$baseUrl/rooms/$encodedRoom/messages?after=$after&limit=100&ignore_fold=1",
            token,
        )
        val eventsJson = body.getJSONArray("events")
        val events = ArrayList<HubMessage>(eventsJson.length())
        for (i in 0 until eventsJson.length()) {
            val event = eventsJson.getJSONObject(i)
            val payload = event.optJSONObject("payload") ?: JSONObject()
            events += HubMessage(
                seq = event.optLong("seq", 0L),
                from = event.optString("from", "unknown"),
                text = payload.optString("text", ""),
                createdAt = event.optString("created_at", ""),
            )
        }
        return PollResult(events, body.optLong("next_cursor", after))
    }

    fun createUiSession(baseUrl: String, token: String): String {
        val connection = (URL("$baseUrl/ui/login").openConnection() as HttpURLConnection).apply {
            requestMethod = "POST"
            instanceFollowRedirects = false
            connectTimeout = CONNECT_TIMEOUT_MS
            readTimeout = READ_TIMEOUT_MS
            doOutput = true
            setRequestProperty("Content-Type", "application/x-www-form-urlencoded; charset=utf-8")
            setRequestProperty("X-Requested-With", "XMLHttpRequest")
        }
        val form = "token=" + URLEncoder.encode(token, Charsets.UTF_8.name())
        connection.outputStream.use { it.write(form.toByteArray(Charsets.UTF_8)) }
        val code = connection.responseCode
        val cookie = connection.headerFields.entries
            .firstOrNull { it.key?.equals("Set-Cookie", ignoreCase = true) == true }
            ?.value
            ?.firstOrNull { it.startsWith("techhub_session=") }
        connection.disconnect()
        if (code !in 300..399 || cookie.isNullOrBlank()) {
            throw IOException("Hub 登录失败（HTTP $code）")
        }
        return cookie
    }

    private fun getJson(url: String, token: String): JSONObject {
        val connection = (URL(url).openConnection() as HttpURLConnection).apply {
            requestMethod = "GET"
            connectTimeout = CONNECT_TIMEOUT_MS
            readTimeout = READ_TIMEOUT_MS
            setRequestProperty("Accept", "application/json")
            setRequestProperty("Authorization", "Bearer $token")
        }
        return try {
            val code = connection.responseCode
            if (code !in 200..299) throw IOException("Hub 请求失败（HTTP $code）")
            val text = connection.inputStream.bufferedReader(Charsets.UTF_8).use { it.readText() }
            JSONObject(text)
        } finally {
            connection.disconnect()
        }
    }
}

