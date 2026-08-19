package com.azhix.techhub

import android.Manifest
import android.app.Activity
import android.app.AlertDialog
import android.content.ActivityNotFoundException
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.view.View
import android.webkit.CookieManager
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.ArrayAdapter
import android.widget.CheckBox
import android.widget.EditText
import android.widget.ImageButton
import android.widget.ProgressBar
import android.widget.Spinner
import android.widget.TextView
import android.widget.Toast
import java.net.URI
import kotlin.concurrent.thread

class MainActivity : Activity() {
    private lateinit var webView: WebView
    private lateinit var statusText: TextView
    private lateinit var pageProgress: ProgressBar
    private lateinit var autoAddressTab: TextView
    private lateinit var homeAddressTab: TextView
    private lateinit var awayAddressTab: TextView
    private var fileChooserCallback: ValueCallback<Array<Uri>>? = null
    @Volatile private var failoverInProgress = false
    @Volatile private var authGeneration = 0L

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        webView = findViewById(R.id.webView)
        statusText = findViewById(R.id.statusText)
        pageProgress = findViewById(R.id.pageProgress)
        autoAddressTab = findViewById(R.id.autoAddressTab)
        homeAddressTab = findViewById(R.id.homeAddressTab)
        awayAddressTab = findViewById(R.id.awayAddressTab)
        configureWebView()

        autoAddressTab.setOnClickListener { selectAddressMode(AddressMode.AUTO) }
        homeAddressTab.setOnClickListener { selectAddressMode(AddressMode.HOME) }
        awayAddressTab.setOnClickListener { selectAddressMode(AddressMode.AWAY) }
        renderAddressTabs()

        findViewById<ImageButton>(R.id.reloadButton).setOnClickListener {
            if (HubConfig.isReady(this)) authenticateAndLoad(force = false)
            else showSettingsDialog(required = true)
        }
        findViewById<ImageButton>(R.id.settingsButton).setOnClickListener {
            showSettingsDialog(required = false)
        }

        NotificationHelper.ensureChannels(this)
        requestNotificationPermissionIfNeeded()
        if (HubConfig.isReady(this)) {
            UnreadPollingService.start(this)
            authenticateAndLoad(force = false)
        } else {
            statusText.setText(R.string.status_waiting)
            showSettingsDialog(required = true)
        }
    }

    override fun onResume() {
        super.onResume()
        NotificationHelper.clearUnread(this)
    }

    override fun onNewIntent(intent: Intent?) {
        super.onNewIntent(intent)
        setIntent(intent)
        if (intent?.getBooleanExtra(EXTRA_OPEN_CHAT, false) == true && HubConfig.isReady(this)) {
            authenticateAndLoad(force = false)
        }
        NotificationHelper.clearUnread(this)
    }

    @Deprecated("Deprecated in Java")
    override fun onBackPressed() {
        if (webView.canGoBack()) webView.goBack() else super.onBackPressed()
    }

    @Deprecated("Deprecated in Java")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        if (requestCode == REQUEST_FILE_CHOOSER) {
            val result = if (resultCode == RESULT_OK) {
                WebChromeClient.FileChooserParams.parseResult(resultCode, data)
            } else {
                null
            }
            fileChooserCallback?.onReceiveValue(result)
            fileChooserCallback = null
            return
        }
        super.onActivityResult(requestCode, resultCode, data)
    }

    override fun onDestroy() {
        fileChooserCallback?.onReceiveValue(null)
        fileChooserCallback = null
        super.onDestroy()
    }

    private fun configureWebView() {
        CookieManager.getInstance().setAcceptCookie(true)
        webView.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            databaseEnabled = true
            allowFileAccess = false
            allowContentAccess = false
            mixedContentMode = android.webkit.WebSettings.MIXED_CONTENT_NEVER_ALLOW
            userAgentString = "$userAgentString TechHubAndroid/0.2.1"
        }
        WebView.setWebContentsDebuggingEnabled(false)
        webView.webChromeClient = object : WebChromeClient() {
            override fun onProgressChanged(view: WebView?, newProgress: Int) {
                pageProgress.progress = newProgress
                pageProgress.visibility = if (newProgress in 1..99) View.VISIBLE else View.GONE
            }

            override fun onShowFileChooser(
                webView: WebView?,
                callback: ValueCallback<Array<Uri>>?,
                fileChooserParams: WebChromeClient.FileChooserParams?,
            ): Boolean {
                callback ?: return false
                fileChooserCallback?.onReceiveValue(null)
                fileChooserCallback = callback

                val pickerIntent = runCatching {
                    fileChooserParams?.createIntent()
                }.getOrNull() ?: Intent(Intent.ACTION_OPEN_DOCUMENT).apply {
                    addCategory(Intent.CATEGORY_OPENABLE)
                    type = "*/*"
                }

                return try {
                    startActivityForResult(
                        Intent.createChooser(pickerIntent, "选择图片或文件"),
                        REQUEST_FILE_CHOOSER,
                    )
                    true
                } catch (_: ActivityNotFoundException) {
                    fileChooserCallback?.onReceiveValue(null)
                    fileChooserCallback = null
                    Toast.makeText(this@MainActivity, "手机上没有可用的文件选择器", Toast.LENGTH_LONG).show()
                    false
                }
            }
        }
        webView.webViewClient = object : WebViewClient() {
            override fun onPageStarted(view: WebView?, url: String?, favicon: Bitmap?) {
                statusText.setText(R.string.status_connecting)
            }

            override fun onPageFinished(view: WebView?, url: String?) {
                val mode = when (HubConfig.addressMode(this@MainActivity)) {
                    AddressMode.AUTO -> "自动"
                    AddressMode.HOME -> "固定"
                    AddressMode.AWAY -> "固定"
                }
                statusText.text = "已连接 · ${HubConfig.profileLabel(this@MainActivity)} · $mode"
                renderAddressTabs()
            }

            override fun onReceivedError(view: WebView?, request: WebResourceRequest?, error: WebResourceError?) {
                if (request?.isForMainFrame != true) return
                statusText.setText(R.string.status_offline)
                if (HubConfig.addressMode(this@MainActivity) == AddressMode.AUTO && !failoverInProgress) {
                    authenticateAndLoad(force = true)
                }
            }

            override fun shouldOverrideUrlLoading(view: WebView?, request: WebResourceRequest?): Boolean {
                val uri = request?.url ?: return false
                if (isHubUrl(uri)) return false
                return runCatching {
                    startActivity(Intent(Intent.ACTION_VIEW, uri))
                    true
                }.getOrDefault(true)
            }
        }
    }

    private fun isHubUrl(uri: Uri): Boolean = HubConfig.candidateUrls(this).any { raw ->
        runCatching {
            val base = URI(raw)
            uri.scheme in listOf("http", "https") &&
                uri.host.equals(base.host, ignoreCase = true) &&
                effectivePort(uri.scheme, uri.port) == effectivePort(base.scheme, base.port)
        }.getOrDefault(false)
    }

    private fun effectivePort(scheme: String?, port: Int): Int = when {
        port >= 0 -> port
        scheme.equals("https", true) -> 443
        else -> 80
    }

    private fun authenticateAndLoad(force: Boolean) {
        if (!HubConfig.isReady(this)) return
        if (failoverInProgress && !force) return
        val generation = ++authGeneration
        val candidates = HubConfig.candidateUrls(this)
        if (!force) {
            val cookieBase = candidates.firstOrNull { baseUrl ->
                CookieManager.getInstance().getCookie(baseUrl).orEmpty().contains("techhub_session=")
            }
            if (cookieBase != null) {
                HubConfig.setActiveBaseUrl(this, cookieBase)
                renderAddressTabs()
                webView.loadUrl("$cookieBase/ui")
                return
            }
        }

        val token = HubConfig.token(this) ?: return
        failoverInProgress = true
        statusText.setText(R.string.status_connecting)
        thread(name = "tech-hub-ui-auth") {
            for (baseUrl in candidates) {
                val cookie = runCatching { HubApi.createUiSession(baseUrl, token) }.getOrNull() ?: continue
                if (generation != authGeneration) return@thread
                runOnUiThread {
                    if (generation != authGeneration) return@runOnUiThread
                    HubConfig.setActiveBaseUrl(this, baseUrl)
                    renderAddressTabs()
                    CookieManager.getInstance().setCookie(baseUrl, cookie) {
                        if (generation != authGeneration) return@setCookie
                        CookieManager.getInstance().flush()
                        failoverInProgress = false
                        webView.loadUrl("$baseUrl/ui")
                    }
                }
                return@thread
            }
            runOnUiThread {
                if (generation != authGeneration) return@runOnUiThread
                failoverInProgress = false
                statusText.setText(R.string.status_offline)
                Toast.makeText(this, "家中和外出地址都无法连接，请检查地址、Tailscale 或 Token", Toast.LENGTH_LONG).show()
            }
        }
    }

    private fun selectAddressMode(mode: AddressMode) {
        if (!HubConfig.setAddressMode(this, mode)) {
            val missing = when (mode) {
                AddressMode.HOME -> "请先在设置里填写家中地址"
                AddressMode.AWAY -> "请先在设置里填写外出地址"
                AddressMode.AUTO -> "请先在设置里填写至少一个地址"
            }
            Toast.makeText(this, missing, Toast.LENGTH_SHORT).show()
            showSettingsDialog(required = false)
            return
        }

        renderAddressTabs()
        statusText.text = when (mode) {
            AddressMode.AUTO -> "正在自动选择地址…"
            AddressMode.HOME -> "正在切换到 Home…"
            AddressMode.AWAY -> "正在切换到 Outside…"
        }
        webView.stopLoading()
        UnreadPollingService.start(this)
        authenticateAndLoad(force = true)
    }

    private fun renderAddressTabs() {
        val mode = HubConfig.addressMode(this)
        val home = HubConfig.homeUrl(this)
        val away = HubConfig.awayUrl(this)
        val active = HubConfig.activeBaseUrl(this)

        autoAddressTab.isEnabled = home.isNotBlank() || away.isNotBlank()
        homeAddressTab.isEnabled = home.isNotBlank()
        awayAddressTab.isEnabled = away.isNotBlank()

        autoAddressTab.isActivated = mode == AddressMode.AUTO
        homeAddressTab.isActivated = mode == AddressMode.HOME
        awayAddressTab.isActivated = mode == AddressMode.AWAY

        autoAddressTab.isSelected = false
        homeAddressTab.isSelected = home.isNotBlank() && active == home
        awayAddressTab.isSelected = away.isNotBlank() && active == away

        val raised = 4f * resources.displayMetrics.density
        autoAddressTab.elevation = if (autoAddressTab.isSelected) raised else 0f
        homeAddressTab.elevation = if (homeAddressTab.isSelected) raised else 0f
        awayAddressTab.elevation = if (awayAddressTab.isSelected) raised else 0f

        autoAddressTab.contentDescription = if (mode == AddressMode.AUTO) "自动切换已开启" else "开启自动切换"
        homeAddressTab.contentDescription = if (homeAddressTab.isSelected) "当前连接家中地址" else "切换到家中地址"
        awayAddressTab.contentDescription = if (awayAddressTab.isSelected) "当前连接外出地址" else "切换到外出地址"
    }

    private fun showSettingsDialog(required: Boolean) {
        val view = layoutInflater.inflate(R.layout.dialog_settings, null)
        val homeInput = view.findViewById<EditText>(R.id.homeUrlInput)
        val awayInput = view.findViewById<EditText>(R.id.awayUrlInput)
        val tokenInput = view.findViewById<EditText>(R.id.tokenInput)
        val modeSpinner = view.findViewById<Spinner>(R.id.modeSpinner)
        val intervalSpinner = view.findViewById<Spinner>(R.id.intervalSpinner)
        val startOnBoot = view.findViewById<CheckBox>(R.id.startOnBootCheck)
        homeInput.setText(HubConfig.homeUrl(this))
        awayInput.setText(HubConfig.awayUrl(this))
        tokenInput.setText(HubConfig.token(this).orEmpty())

        val modeLabels = listOf("自动切换（推荐）", "固定家中地址", "固定外出地址")
        modeSpinner.adapter = ArrayAdapter(this, android.R.layout.simple_spinner_dropdown_item, modeLabels)
        modeSpinner.setSelection(HubConfig.addressMode(this).ordinal)
        val intervalLabels = listOf("1 分钟", "2 分钟（推荐）", "3 分钟")
        intervalSpinner.adapter = ArrayAdapter(this, android.R.layout.simple_spinner_dropdown_item, intervalLabels)
        intervalSpinner.setSelection((HubConfig.intervalMinutes(this) - 1L).toInt())
        startOnBoot.isChecked = HubConfig.startOnBoot(this)

        val dialog = AlertDialog.Builder(this)
            .setTitle("连接 tech-hub")
            .setView(view)
            .setCancelable(!required)
            .setNegativeButton(if (required) null else "取消", null)
            .setPositiveButton("保存", null)
            .create()
        dialog.setOnShowListener {
            dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener {
                val token = tokenInput.text.toString()
                try {
                    require(token.isNotBlank()) { "Token 不能为空" }
                    val profilesChanged = HubConfig.saveProfiles(
                        this,
                        homeInput.text.toString(),
                        awayInput.text.toString(),
                        AddressMode.entries[modeSpinner.selectedItemPosition],
                        token,
                        intervalSpinner.selectedItemPosition + 1L,
                        startOnBoot.isChecked,
                    )
                    if (profilesChanged) CookieManager.getInstance().flush()
                    renderAddressTabs()
                    UnreadPollingService.start(this)
                    authenticateAndLoad(force = true)
                    dialog.dismiss()
                } catch (error: Exception) {
                    homeInput.error = error.message ?: "配置无效"
                }
            }
        }
        dialog.show()
    }

    private fun requestNotificationPermissionIfNeeded() {
        if (Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), REQUEST_NOTIFICATIONS)
        }
    }

    companion object {
        const val EXTRA_OPEN_CHAT = "open_chat"
        private const val REQUEST_NOTIFICATIONS = 7120
        private const val REQUEST_FILE_CHOOSER = 7121
    }
}

