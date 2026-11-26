package com.vtoh.vtoh

import android.content.Context
import android.graphics.Color
import android.graphics.PixelFormat
import android.graphics.drawable.GradientDrawable
import android.media.AudioAttributes
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.speech.tts.TextToSpeech
import android.speech.tts.UtteranceProgressListener
import android.text.TextUtils
import android.view.Gravity
import android.view.MotionEvent
import android.view.View
import android.view.ViewTreeObserver
import android.view.WindowManager
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import java.util.Locale

internal class FloatingCaption(
    private val context: Context, private val closed: () -> Unit, private val stop: () -> Unit
) {
    private val manager = context.getSystemService(WindowManager::class.java)
    private val prefs = context.getSharedPreferences("vtoh_display", Context.MODE_PRIVATE)
    private val styleColors = mapOf("standard" to 0xD6000000.toInt(),
        "light" to 0xAB000000.toInt(), "contrast" to 0xF5000000.toInt())
    private var style = prefs.getString("overlay_style", "standard").orEmpty()
    private var mode = prefs.getString("overlay_mode", "bilingual").orEmpty()
    private var fontSize = prefs.getInt("overlay_font_size", 18)
    private val history = linkedMapOf<String, CaptionRow>()
    private val visibleRows = mutableMapOf<String, VisibleRow>()
    private var scroll: ScrollView? = null
    private var list: LinearLayout? = null
    private var empty: TextView? = null
    private var followPending = false
    private var canStop = false
    private var stopButton: TextView? = null
    private var root: LinearLayout? = null
    private val main = Handler(Looper.getMainLooper())
    private var speechButton: TextView? = null
    private var speechEngine: TextToSpeech? = null
    private var speechReady = false
    private var speechRequest: SpeechRequest? = null
    private var speechRevision = 0
    private var selection: Selection? = null

    private data class CaptionRow(val id: String, val original: List<String>,
        val translation: List<String>, val language: String)
    private data class VisibleRow(val container: LinearLayout, val original: LinearLayout,
        val translation: LinearLayout)
    private data class Selection(val id: String, val translated: Boolean, val index: Int, val text: String)
    private data class SpeechRequest(val text: String, val locale: Locale, val id: String)

    private fun dp(size: Int) = (context.resources.displayMetrics.density * size).toInt()

    fun show() {
        check(Settings.canDrawOverlays(context))
        if (root != null) return
        val panel = LinearLayout(context).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(14), dp(5), dp(14), dp(10))
            background = panelBackground()
        }
        val params = WindowManager.LayoutParams(
            minOf(dp(360), context.resources.displayMetrics.widthPixels - dp(24)),
            dp(220),
            WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE,
            PixelFormat.TRANSLUCENT,
        ).apply { gravity = Gravity.TOP or Gravity.LEFT; x = dp(12); y = dp(54) }
        val bar = LinearLayout(context).apply { gravity = Gravity.CENTER_VERTICAL }
        val handle = label("VtoH · 悬浮字幕", 12, 0xffdddddd.toInt()).apply {
            setPadding(0, dp(8), 0, dp(8))
            var initialX = 0
            var initialY = 0
            var startX = 0f
            var startY = 0f
            setOnTouchListener { _, event ->
                when (event.actionMasked) {
                    MotionEvent.ACTION_DOWN -> {
                        initialX = params.x
                        initialY = params.y
                        startX = event.rawX
                        startY = event.rawY
                    }
                    MotionEvent.ACTION_MOVE -> {
                        val metrics = context.resources.displayMetrics
                        params.x = (initialX + (event.rawX - startX).toInt())
                            .coerceIn(0, (metrics.widthPixels - panel.width).coerceAtLeast(0))
                        params.y = (initialY + (event.rawY - startY).toInt())
                            .coerceIn(0, (metrics.heightPixels - panel.height).coerceAtLeast(0))
                        manager.updateViewLayout(panel, params)
                    }
                    MotionEvent.ACTION_UP -> performClick()
                }
                true
            }
        }
        val dismiss = label("×", 23, Color.WHITE).apply {
            contentDescription = "收起悬浮字幕"
            gravity = Gravity.CENTER
            setOnClickListener { hide(); closed() }
        }
        bar.addView(handle, LinearLayout.LayoutParams(0, -2, 1f))
        val stopAction = label("停止监听", 12, Color.WHITE).apply {
            gravity = Gravity.CENTER
            setOnClickListener { stop() }
        }
        bar.addView(stopAction, LinearLayout.LayoutParams(dp(70), dp(42)))
        bar.addView(dismiss, LinearLayout.LayoutParams(dp(42), dp(42)))
        panel.addView(bar)
        val scroller = ScrollView(context).apply {
            isVerticalScrollBarEnabled = true
            isScrollbarFadingEnabled = false
            overScrollMode = View.OVER_SCROLL_IF_CONTENT_SCROLLS
            contentDescription = "悬浮字幕历史"
        }
        val rows = LinearLayout(context).apply { orientation = LinearLayout.VERTICAL }
        val placeholder = label("等待实时字幕", 16, Color.WHITE)
        rows.addView(placeholder)
        scroller.addView(rows)
        panel.addView(scroller, LinearLayout.LayoutParams(-1, 0, 1f))
        val controls = LinearLayout(context).apply { gravity = Gravity.END or Gravity.CENTER_VERTICAL }
        val modeButton = label(if (mode == "translation") "仅译文" else "双语", 12, Color.WHITE)
        modeButton.gravity = Gravity.CENTER
        modeButton.setOnClickListener {
            setMode(if (mode == "translation") "bilingual" else "translation")
            modeButton.text = if (mode == "translation") "仅译文" else "双语"
        }
        val speakAction = label("朗读", 12, Color.WHITE).apply {
            gravity = Gravity.CENTER
            setOnClickListener { if (speechRequest == null) readSelection() else stopSpeech() }
        }
        val resize = label("↘", 22, Color.WHITE).apply {
            gravity = Gravity.CENTER
            contentDescription = "拖动缩放悬浮字幕"
            var startX = 0f
            var startY = 0f
            var initialWidth = 0
            var initialHeight = 0
            setOnTouchListener { _, event ->
                when (event.actionMasked) {
                    MotionEvent.ACTION_DOWN -> {
                        startX = event.rawX
                        startY = event.rawY
                        initialWidth = params.width
                        initialHeight = params.height
                    }
                    MotionEvent.ACTION_MOVE -> {
                        val maxWidth = (context.resources.displayMetrics.widthPixels - params.x).coerceAtLeast(dp(180))
                        params.width = (initialWidth + (event.rawX - startX).toInt()).coerceIn(dp(180), maxWidth)
                        val maxHeight = (context.resources.displayMetrics.heightPixels - params.y)
                            .coerceAtLeast(dp(145))
                        params.height = (initialHeight + (event.rawY - startY).toInt())
                            .coerceIn(dp(145), maxHeight)
                        manager.updateViewLayout(panel, params)
                    }
                    MotionEvent.ACTION_UP -> performClick()
                }
                true
            }
        }
        controls.addView(speakAction, LinearLayout.LayoutParams(dp(58), dp(42)))
        controls.addView(modeButton, LinearLayout.LayoutParams(dp(52), dp(42)))
        controls.addView(resize, LinearLayout.LayoutParams(dp(42), dp(42)))
        panel.addView(controls)
        manager.addView(panel, params)
        root = panel
        scroll = scroller
        list = rows
        empty = placeholder
        stopButton = stopAction
        speechButton = speakAction
        history.values.forEach(::renderRow)
        renderEmpty()
        updateSpeechButton()
        updateStop(canStop)
        follow(scroller)
    }

    fun updateStop(stoppable: Boolean) {
        canStop = stoppable
        stopButton?.visibility = if (canStop) View.VISIBLE else View.GONE
    }

    fun updateHistory(rows: List<Map<String, Any>>, reset: Boolean) {
        val viewport = scroll
        val autoFollow = viewport != null && (reset || followPending || !viewport.canScrollVertically(1))
        if (reset) {
            select(null)
            stopSpeech()
            history.clear()
            visibleRows.values.forEach { list?.removeView(it.container) }
            visibleRows.clear()
        }
        for (data in rows) {
            val id = data["id"] as? String ?: continue
            val row = CaptionRow(id, segments(data["text_lines"]), segments(data["translation_lines"]),
                data["translation_language"] as? String ?: "")
            history[id] = row
            renderRow(row)
        }
        renderEmpty()
        updateSpeechButton()
        if (viewport != null && autoFollow) follow(viewport)
    }

    private fun segments(value: Any?): List<String> =
        (value as? List<*>).orEmpty().filterIsInstance<String>().map(String::trim).filter(String::isNotEmpty)

    private fun follow(viewport: ScrollView) {
        if (followPending) return
        followPending = true
        viewport.viewTreeObserver.addOnGlobalLayoutListener(object : ViewTreeObserver.OnGlobalLayoutListener {
            override fun onGlobalLayout() {
                viewport.viewTreeObserver.removeOnGlobalLayoutListener(this)
                if (scroll === viewport) {
                    viewport.scrollTo(0, (viewport.getChildAt(0).height - viewport.height).coerceAtLeast(0))
                    followPending = false
                }
            }
        })
        viewport.requestLayout()
    }

    private fun setMode(value: String) {
        val viewport = scroll
        val autoFollow = viewport != null && (followPending || !viewport.canScrollVertically(1))
        prefs.edit().putString("overlay_mode", value).apply()
        mode = value
        if (mode == "translation" && selection?.translated == false) select(null)
        stopSpeech()
        for (row in history.values) renderRow(row)
        renderEmpty()
        if (viewport != null && autoFollow) follow(viewport)
    }

    private fun renderRow(row: CaptionRow) {
        val container = list ?: return
        val (line, original, translation) = visibleRows.getOrPut(row.id) {
            val line = LinearLayout(context).apply {
                orientation = LinearLayout.VERTICAL
                setPadding(0, dp(4), dp(8), dp(7))
            }
            val original = LinearLayout(context).apply { orientation = LinearLayout.VERTICAL }
            val translation = LinearLayout(context).apply { orientation = LinearLayout.VERTICAL }
            line.addView(original)
            line.addView(translation)
            container.addView(line)
            VisibleRow(line, original, translation)
        }
        val only = mode == "translation"
        renderSegments(row.id, original, row.original, translated = false)
        renderSegments(row.id, translation, row.translation, translated = true)
        original.visibility = if (only) View.GONE else View.VISIBLE
        translation.visibility = if (row.translation.isEmpty()) View.GONE else View.VISIBLE
        line.visibility = if (only && row.translation.isEmpty()) View.GONE else View.VISIBLE
    }

    private fun renderSegments(id: String, group: LinearLayout, lines: List<String>, translated: Boolean) {
        val selected = selection
        if (selected?.id == id && selected.translated == translated &&
            lines.getOrNull(selected.index) != selected.text) select(null)
        while (group.childCount > lines.size) group.removeViewAt(group.childCount - 1)
        for ((index, text) in lines.withIndex()) {
            val view = group.getChildAt(index) as? TextView ?: label("", captionSize(translated),
                if (translated) 0xffffdf9f.toInt() else Color.WHITE).apply {
                maxLines = Int.MAX_VALUE
                ellipsize = null
                setPadding(dp(6), dp(3), dp(6), dp(3))
                setOnClickListener { select(Selection(id, translated, index, this.text.toString())) }
                group.addView(this, LinearLayout.LayoutParams(-1, -2))
            }
            if (view.text.toString() != text) view.text = text
        }
    }

    private fun select(value: Selection?) {
        paintSelection(selection, false)
        selection = value
        paintSelection(value, true)
        updateSpeechButton()
    }

    private fun paintSelection(target: Selection?, selected: Boolean) {
        if (target == null) return
        val row = visibleRows[target.id] ?: return
        val group = if (target.translated) row.translation else row.original
        group.getChildAt(target.index)?.apply {
            isSelected = selected
            setBackgroundColor(if (selected) 0xff364740.toInt() else Color.TRANSPARENT)
        }
    }

    private fun renderEmpty() {
        empty?.apply {
            val only = mode == "translation"
            val hasRows = if (only) history.values.any { it.translation.isNotEmpty() } else history.isNotEmpty()
            text = if (only) "等待译文 · 请在主界面选择目标语言" else "等待实时字幕"
            visibility = if (hasRows) View.GONE else View.VISIBLE
        }
    }

    fun style() = style

    fun font() = fontSize

    private fun captionSize(translated: Boolean) =
        if (translated) (fontSize - 1).coerceAtLeast(12) else fontSize

    fun setFont(value: Int) {
        require(value in 12..36)
        prefs.edit().putInt("overlay_font_size", value).apply()
        fontSize = value
        for (row in visibleRows.values) {
            for (index in 0 until row.original.childCount) {
                (row.original.getChildAt(index) as TextView).textSize = captionSize(false).toFloat()
            }
            for (index in 0 until row.translation.childCount) {
                (row.translation.getChildAt(index) as TextView).textSize = captionSize(true).toFloat()
            }
        }
    }

    fun setStyle(value: String) {
        require(value in styleColors)
        check(prefs.edit().putString("overlay_style", value).commit())
        style = value
        root?.background = panelBackground()
    }

    private fun panelBackground() = GradientDrawable().apply {
        cornerRadius = dp(16).toFloat()
        setColor(styleColors[style] ?: styleColors.getValue("standard"))
    }

    fun hide() {
        select(null)
        stopSpeech()
        root?.let { manager.removeViewImmediate(it) }
        root = null
        scroll = null
        list = null
        empty = null
        followPending = false
        visibleRows.clear()
        stopButton = null
        speechButton = null
    }

    fun dispose() {
        hide()
        speechEngine?.shutdown()
        speechEngine = null
        speechReady = false
    }

    private fun updateSpeechButton() {
        speechButton?.apply {
            text = when {
                speechRequest != null -> "停止朗读"
                selection != null -> "朗读"
                else -> "选句朗读"
            }
            contentDescription = if (selection == null && speechRequest == null) "选择一句字幕后朗读" else text
            isEnabled = speechRequest != null || selection != null
            alpha = if (isEnabled) 1f else .5f
        }
    }

    private fun readSelection() {
        val target = selection ?: return
        val row = history[target.id] ?: return
        val text = target.text
        val language = if (target.translated) row.language else ""
        val locale = when {
            language.isNotBlank() -> Locale.forLanguageTag(language)
            text.any { it in '\u3040'..'\u30ff' } -> Locale.JAPANESE
            text.any { it in '\uac00'..'\ud7af' } -> Locale.KOREAN
            text.any { it in '\u3400'..'\u9fff' } -> Locale.SIMPLIFIED_CHINESE
            else -> Locale.getDefault()
        }
        speechRequest = SpeechRequest(text, locale, (++speechRevision).toString())
        updateSpeechButton()
        if (speechReady) {
            playSpeech()
        } else if (speechEngine == null) {
            speechEngine = TextToSpeech(context) { status ->
                main.post {
                    val engine = speechEngine ?: return@post
                    if (status != TextToSpeech.SUCCESS) {
                        engine.shutdown()
                        speechEngine = null
                        if (speechRequest != null) speechError("无法使用系统朗读，请检查文字转语音设置。")
                        return@post
                    }
                    engine.setAudioAttributes(AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_ASSISTANCE_ACCESSIBILITY)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                        .build())
                    speechReady = true
                    playSpeech()
                }
            }
        }
    }

    private fun playSpeech() {
        val request = speechRequest ?: return
        val engine = speechEngine ?: return
        if (engine.setLanguage(request.locale) < TextToSpeech.LANG_AVAILABLE) {
            speechError("请在系统设置中安装对应的朗读语音。")
            return
        }
        engine.setOnUtteranceProgressListener(object : UtteranceProgressListener() {
            override fun onStart(utteranceId: String) = Unit
            override fun onDone(utteranceId: String) {
                main.post { if (speechRequest?.id == utteranceId) stopSpeech() }
            }
            override fun onError(utteranceId: String) {
                main.post { if (speechRequest?.id == utteranceId) speechError("无法朗读这段字幕。") }
            }
        })
        if (engine.speak(request.text, TextToSpeech.QUEUE_FLUSH, null, request.id) != TextToSpeech.SUCCESS) {
            speechError("无法朗读这段字幕。")
        }
    }

    private fun speechError(message: String) {
        stopSpeech()
        Toast.makeText(context, message, Toast.LENGTH_SHORT).show()
    }

    private fun stopSpeech() {
        speechRequest = null
        if (speechReady) speechEngine?.stop()
        updateSpeechButton()
    }

    private fun label(value: String, size: Int, color: Int) = TextView(context).apply {
        text = value
        textSize = size.toFloat()
        setTextColor(color)
        maxLines = 2
        ellipsize = TextUtils.TruncateAt.END
    }
}
