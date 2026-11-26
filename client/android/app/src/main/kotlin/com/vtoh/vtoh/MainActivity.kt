package com.vtoh.vtoh

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.media.projection.MediaProjectionManager
import android.net.Uri
import android.provider.Settings
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.EventChannel
import io.flutter.plugin.common.MethodChannel

class MainActivity : FlutterActivity() {
    private var pending: MethodChannel.Result? = null
    private var requestedSource = "mic"
    private var overlayPermission: MethodChannel.Result? = null
    private lateinit var caption: FloatingCaption

    override fun configureFlutterEngine(engine: FlutterEngine) {
        super.configureFlutterEngine(engine)
        val messenger = engine.dartExecutor.binaryMessenger
        val windowChannel = MethodChannel(messenger, "vtoh/workspace")
        val store = ConnectionStore(applicationContext)
        caption = FloatingCaption(
            applicationContext,
            { windowChannel.invokeMethod("overlayClosed", null) },
            {
                fail("已停止监听")
                stopService(Intent(this, CaptureService::class.java))
                windowChannel.invokeMethod("overlayStop", null)
            }
        )
        windowChannel.setMethodCallHandler { call, result ->
            try {
                when (call.method) {
                    "loadConnection" -> result.success(store.read())
                    "saveConnection" -> {
                        store.write(requireNotNull(call.argument<String>("key")))
                        result.success(null)
                    }
                    "showOverlay" -> {
                        if (Settings.canDrawOverlays(this)) {
                            caption.show()
                            result.success(true)
                        } else if (overlayPermission == null) {
                            startActivityForResult(Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                                Uri.parse("package:$packageName")), OVERLAY)
                            overlayPermission = result
                        } else {
                            result.success(false)
                        }
                    }
                    "hideOverlay" -> { caption.hide(); result.success(null) }
                    "overlayStyle" -> result.success(caption.style())
                    "overlayFont" -> result.success(caption.font())
                    "setOverlayFont" -> { caption.setFont(requireNotNull(call.arguments as? Int)); result.success(null) }
                    "setOverlayStyle" -> {
                        caption.setStyle(requireNotNull(call.arguments as? String))
                        result.success(null)
                    }
                    "updateOverlay" -> {
                        caption.updateStop(call.argument<Boolean>("canStop") == true)
                        result.success(null)
                    }
                    "updateOverlayHistory" -> {
                        caption.updateHistory(
                            call.argument<List<Map<String, Any>>>("rows").orEmpty(),
                            call.argument<Boolean>("reset") == true
                        )
                        result.success(null)
                    }
                    else -> result.notImplemented()
                }
            } catch (_: Exception) {
                result.error("workspace", "无法操作悬浮窗或安全存储", null)
            }
        }
        EventChannel(messenger, "vtoh/frames").setStreamHandler(object : EventChannel.StreamHandler {
            override fun onListen(arguments: Any?, events: EventChannel.EventSink) {
                CaptureService.events = events
            }

            override fun onCancel(arguments: Any?) {
                CaptureService.events = null
                stopService(Intent(this@MainActivity, CaptureService::class.java))
            }
        })
        MethodChannel(messenger, "vtoh/capture").setMethodCallHandler { call, result ->
            when (call.method) {
                "start" -> {
                    val source = call.arguments as? String
                    if (pending != null || CaptureService.started || source !in setOf("mic", "playback")) {
                        result.error("invalid", "当前无法启动音频采集", null)
                        return@setMethodCallHandler
                    }
                    pending = result
                    requestedSource = source!!
                    if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) {
                        askForSource()
                    } else {
                        requestPermissions(arrayOf(Manifest.permission.RECORD_AUDIO), RECORD_AUDIO)
                    }
                }
                "stop" -> {
                    fail("已停止监听")
                    stopService(Intent(this, CaptureService::class.java))
                    result.success(null)
                }
                else -> result.notImplemented()
            }
        }
    }

    private fun askForSource() {
        if (requestedSource == "mic") {
            begin(null)
        } else {
            val manager = getSystemService(MediaProjectionManager::class.java)
            startActivityForResult(manager.createScreenCaptureIntent(), SHARE_PLAYBACK)
        }
    }

    private fun begin(grant: Intent?) {
        val result = pending ?: return
        pending = null
        val intent = Intent(this, CaptureService::class.java).putExtra("source", requestedSource)
        if (grant != null) intent.putExtra("projection", grant)
        try {
            startForegroundService(intent)
            result.success(null)
        } catch (failure: RuntimeException) {
            result.error("start", "Android 拒绝启动采集服务", null)
        }
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<String>, results: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, results)
        if (requestCode != RECORD_AUDIO || pending == null) return
        if (results.firstOrNull() == PackageManager.PERMISSION_GRANTED) askForSource()
        else fail("需要录音权限才能采集声音")
    }

    @Deprecated("Android requests projection consent through an activity result")
    override fun onActivityResult(request: Int, result: Int, data: Intent?) {
        super.onActivityResult(request, result, data)
        if (request == OVERLAY) {
            val pendingResult = overlayPermission ?: return
            overlayPermission = null
            try {
                val allowed = Settings.canDrawOverlays(this)
                if (allowed) caption.show()
                pendingResult.success(allowed)
            } catch (_: RuntimeException) {
                pendingResult.error("overlay", "无法显示悬浮字幕", null)
            }
            return
        }
        if (request != SHARE_PLAYBACK) return
        if (result == Activity.RESULT_OK && data != null) begin(data)
        else fail("已取消应用播放声音授权")
    }

    private fun fail(message: String) {
        pending?.error("cancelled", message, null)
        pending = null
    }

    override fun onDestroy() {
        fail("采集窗口已关闭")
        overlayPermission?.success(false)
        overlayPermission = null
        if (::caption.isInitialized) caption.dispose()
        super.onDestroy()
    }

    private companion object {
        const val RECORD_AUDIO = 102
        const val SHARE_PLAYBACK = 103
        const val OVERLAY = 104
    }
}
