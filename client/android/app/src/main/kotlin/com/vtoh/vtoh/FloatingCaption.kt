package com.vtoh.vtoh

import android.content.Context
import android.graphics.Color
import android.graphics.PixelFormat
import android.graphics.drawable.GradientDrawable
import android.provider.Settings
import android.text.TextUtils
import android.view.Gravity
import android.view.MotionEvent
import android.view.View
import android.view.ViewTreeObserver
import android.view.WindowManager
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView

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
    private val history = linkedMapOf<String, Map<String, String>>()
    private val visibleRows = mutableMapOf<String, Triple<LinearLayout, TextView, TextView>>()
    private var scroll: ScrollView? = null
    private var list: LinearLayout? = null
    private var empty: TextView? = null
    private var followPending = false
    private var canStop = false
    private var stopButton: TextView? = null
    private var root: LinearLayout? = null

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
        val modeButton = label(if (mode == "translation") "仅译文" else "原文＋译文", 12, Color.WHITE)
        modeButton.setPadding(dp(10), dp(8), dp(10), dp(8))
        modeButton.setOnClickListener {
            setMode(if (mode == "translation") "bilingual" else "translation")
            modeButton.text = if (mode == "translation") "仅译文" else "原文＋译文"
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
        controls.addView(modeButton)
        controls.addView(resize, LinearLayout.LayoutParams(dp(42), dp(42)))
        panel.addView(controls)
        manager.addView(panel, params)
        root = panel
        scroll = scroller
        list = rows
        empty = placeholder
        stopButton = stopAction
        history.values.forEach(::renderRow)
        renderEmpty()
        updateStop(canStop)
        follow(scroller)
    }

    fun updateStop(stoppable: Boolean) {
        canStop = stoppable
        stopButton?.visibility = if (canStop) View.VISIBLE else View.GONE
    }

    fun updateHistory(rows: List<Map<String, String>>, reset: Boolean) {
        val viewport = scroll
        val autoFollow = viewport != null && (reset || followPending || !viewport.canScrollVertically(1))
        if (reset) {
            history.clear()
            visibleRows.values.forEach { list?.removeView(it.first) }
            visibleRows.clear()
        }
        for (row in rows) {
            val id = row["id"] ?: continue
            history[id] = row
            renderRow(row)
        }
        renderEmpty()
        if (viewport != null && autoFollow) follow(viewport)
    }

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
        for (row in history.values) renderRow(row)
        renderEmpty()
        if (viewport != null && autoFollow) follow(viewport)
    }

    private fun renderRow(row: Map<String, String>) {
        val container = list ?: return
        val id = row.getValue("id")
        val (line, original, translation) = visibleRows.getOrPut(id) {
            val line = LinearLayout(context).apply {
                orientation = LinearLayout.VERTICAL
                setPadding(0, dp(4), dp(8), dp(7))
            }
            val original = label("", fontSize, Color.WHITE).apply { maxLines = Int.MAX_VALUE; ellipsize = null }
            val translation = label("", (fontSize - 1).coerceAtLeast(12), 0xffffdf9f.toInt()).apply { maxLines = Int.MAX_VALUE; ellipsize = null }
            line.addView(original)
            line.addView(translation)
            container.addView(line)
            Triple(line, original, translation)
        }
        val only = mode == "translation"
        val translated = row["translation"].orEmpty()
        original.text = row["text"].orEmpty()
        original.visibility = if (only) View.GONE else View.VISIBLE
        translation.text = translated
        translation.visibility = if (translated.isEmpty()) View.GONE else View.VISIBLE
        line.visibility = if (only && translated.isEmpty()) View.GONE else View.VISIBLE
    }

    private fun renderEmpty() {
        empty?.apply {
            val only = mode == "translation"
            val hasRows = if (only) history.values.any { !it["translation"].isNullOrEmpty() } else history.isNotEmpty()
            text = if (only) "等待译文 · 请在主界面选择目标语言" else "等待实时字幕"
            visibility = if (hasRows) View.GONE else View.VISIBLE
        }
    }

    fun style() = style

    fun font() = fontSize

    fun setFont(value: Int) {
        require(value in 12..36)
        prefs.edit().putInt("overlay_font_size", value).apply()
        fontSize = value
        visibleRows.values.forEach { it.second.textSize = value.toFloat(); it.third.textSize = (value - 1).coerceAtLeast(12).toFloat() }
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
        root?.let { manager.removeViewImmediate(it) }
        root = null
        scroll = null
        list = null
        empty = null
        followPending = false
        visibleRows.clear()
        stopButton = null
    }

    private fun label(value: String, size: Int, color: Int) = TextView(context).apply {
        text = value
        textSize = size.toFloat()
        setTextColor(color)
        maxLines = 2
        ellipsize = TextUtils.TruncateAt.END
    }
}
