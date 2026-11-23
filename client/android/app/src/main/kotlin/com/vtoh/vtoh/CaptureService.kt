package com.vtoh.vtoh

import android.annotation.SuppressLint
import android.app.Activity
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.content.pm.ServiceInfo
import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioPlaybackCaptureConfiguration
import android.media.AudioRecord
import android.media.MediaRecorder
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import io.flutter.plugin.common.EventChannel

class CaptureService : Service() {
    private val main = Handler(Looper.getMainLooper())
    private var projection: MediaProjection? = null
    private var recorder: AudioRecord? = null
    private var reader: Thread? = null
    @Volatile private var running = false
    private var notified = false
    private val projectionCallback = object : MediaProjection.Callback() {
        override fun onStop() = finish("应用播放声音授权已结束")
    }

    override fun onBind(intent: Intent?): IBinder? = null

    @SuppressLint("MissingPermission")
    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == STOP) {
            finish("已停止监听")
            return START_NOT_STICKY
        }
        if (intent == null || started) return START_NOT_STICKY
        started = true
        val source = intent.getStringExtra("source")
        val playback = source == "playback"
        try {
            val type = when {
                playback -> ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION
                Build.VERSION.SDK_INT >= 30 -> ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE
                else -> ServiceInfo.FOREGROUND_SERVICE_TYPE_NONE
            }
            startForeground(1, notification(source), type)
            val format = AudioFormat.Builder()
                .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                .setSampleRate(SAMPLE_RATE)
                .setChannelMask(AudioFormat.CHANNEL_IN_MONO)
                .build()
            val minimum = AudioRecord.getMinBufferSize(
                SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT
            )
            check(minimum > 0)
            val builder = AudioRecord.Builder()
                .setAudioFormat(format)
                .setBufferSizeInBytes(maxOf(minimum * 2, FRAME_BYTES * 4))
            if (playback) {
                @Suppress("DEPRECATION")
                val grant = if (Build.VERSION.SDK_INT >= 33) {
                    intent.getParcelableExtra("projection", Intent::class.java)
                } else {
                    intent.getParcelableExtra<Intent>("projection")
                }
                val manager = getSystemService(MediaProjectionManager::class.java)
                val capture = checkNotNull(manager.getMediaProjection(Activity.RESULT_OK, checkNotNull(grant)))
                projection = capture
                capture.registerCallback(projectionCallback, main)
                val configuration = AudioPlaybackCaptureConfiguration.Builder(capture)
                    .addMatchingUsage(AudioAttributes.USAGE_MEDIA)
                    .addMatchingUsage(AudioAttributes.USAGE_GAME)
                    .addMatchingUsage(AudioAttributes.USAGE_UNKNOWN)
                    .build()
                builder.setAudioPlaybackCaptureConfig(configuration)
            } else {
                builder.setAudioSource(MediaRecorder.AudioSource.MIC)
            }
            val input = builder.build()
            recorder = input
            check(input.state == AudioRecord.STATE_INITIALIZED)
            input.startRecording()
            check(input.recordingState == AudioRecord.RECORDSTATE_RECORDING)
            running = true
            reader = Thread({ readFrames(input) }, "VtoH PCM reader").also { it.start() }
        } catch (_: RuntimeException) {
            finish("无法采集所选音源，请检查录音权限或切换到麦克风")
        }
        return START_NOT_STICKY
    }

    private fun readFrames(input: AudioRecord) {
        val bytes = ByteArray(FRAME_BYTES)
        while (running) {
            val count = input.read(bytes, 0, bytes.size, AudioRecord.READ_BLOCKING)
            if (count > 0) {
                val frame = bytes.copyOf(count)
                main.post { if (running) events?.success(frame) }
            } else {
                if (running) main.post { finish("音频采集已中断") }
                break
            }
        }
    }

    private fun notification(source: String?): Notification {
        val manager = getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(
            NotificationChannel(CHANNEL, "音频采集", NotificationManager.IMPORTANCE_LOW)
        )
        val stop = Intent(this, CaptureService::class.java).setAction(STOP)
        val stopAction = PendingIntent.getService(this, 0, stop, PendingIntent.FLAG_IMMUTABLE)
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE
        )
        return Notification.Builder(this, CHANNEL)
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setContentTitle("VtoH 正在监听")
            .setContentText(if (source == "playback") "应用播放声音" else "麦克风")
            .setContentIntent(open)
            .setOngoing(true)
            .addAction(Notification.Action.Builder(null, "停止", stopAction).build())
            .build()
    }

    private fun finish(message: String) {
        if (!notified) {
            notified = true
            events?.success(message)
        }
        stopSelf()
    }

    override fun onDestroy() {
        running = false
        recorder?.let { input ->
            try {
                if (input.recordingState == AudioRecord.RECORDSTATE_RECORDING) input.stop()
                reader?.join(400)
            } finally {
                input.release()
            }
        }
        recorder = null
        reader = null
        projection?.unregisterCallback(projectionCallback)
        projection?.stop()
        projection = null
        started = false
        stopForeground(STOP_FOREGROUND_REMOVE)
        super.onDestroy()
    }

    companion object {
        private const val CHANNEL = "vtoh_capture"
        private const val STOP = "com.vtoh.vtoh.STOP"
        private const val SAMPLE_RATE = 16000
        private const val FRAME_BYTES = 3200
        var events: EventChannel.EventSink? = null
        var started = false
            private set
    }
}
