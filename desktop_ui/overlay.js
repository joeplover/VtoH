'use strict';

window.addEventListener('pywebviewready', async () => {
  const api = window.pywebview.api;
  const stream = document.getElementById('stream');
  const captions = document.getElementById('captions');
  const empty = document.getElementById('empty');
  const style = document.getElementById('style');
  const mode = document.getElementById('mode');
  const toggle = document.getElementById('toggle');
  const hide = document.getElementById('hide');
  const resize = document.getElementById('resize');
  const font = document.getElementById('font');
  const speak = document.getElementById('speak');
  const speechError = document.getElementById('speech-error');
  const rows = new Map();
  let revision = -1;
  let session = null;
  let drag;
  let selection = null;
  let speech = {speaking: false, error: ''};
  let requestingSpeech = false;
  function selectCaption(next) {
    selection?.button.setAttribute('aria-pressed', 'false');
    selection = next;
    selection?.button.setAttribute('aria-pressed', 'true');
    renderSpeech();
  }
  function renderSpeech() {
    speak.textContent = speech.speaking ? '停止朗读' : selection ? '朗读' : '选句朗读';
    speak.title = speech.speaking ? '停止朗读' : selection ? '朗读选中字幕' : '选择一句字幕后朗读';
    speak.disabled = requestingSpeech || (!speech.speaking && !selection);
    speechError.textContent = speech.error;
    speechError.hidden = !speech.error;
  }
  speak.onclick = async () => {
    requestingSpeech = true;
    renderSpeech();
    try {
      if (speech.speaking) {
        await api.stop_caption_speech();
      } else if (selection) {
        const {id, field, text} = selection;
        await api.speak_caption(id, field, text);
      }
      speech = await api.caption_speech_status();
    } finally {
      requestingSpeech = false;
      renderSpeech();
    }
  };
  hide.onclick = async () => {
    hide.disabled = true;
    try {
      await api.hide_overlay();
    } finally {
      hide.disabled = false;
    }
  };
  hide.disabled = false;
  style.value = await api.overlay_style();
  mode.value = await api.overlay_mode();
  const savedFont = await api.overlay_font();
  font.value = savedFont.family + '-' + savedFont.size;
  function applyFont() {
    const parts = font.value.split('-');
    captions.classList.toggle('font-serif', parts[0] === 'serif');
    captions.classList.toggle('font-mono', parts[0] === 'mono');
    captions.style.fontSize = parts[1] + 'px';
  }
  font.onchange = async () => {
    const parts = font.value.split('-');
    if (!await api.set_overlay_font(Number(parts[1]), parts[0])) {
      font.value = savedFont.family + '-' + savedFont.size;
    }
    applyFont();
  };
  style.onchange = async () => {
    if (!await api.set_overlay_style(style.value)) style.value = await api.overlay_style();
  };
  function atBottom() {
    return stream.scrollHeight - stream.clientHeight - stream.scrollTop < 8;
  }
  function scrollToLatest() {
    stream.scrollTop = stream.scrollHeight;
  }
  function renderMode() {
    const follow = atBottom();
    const only = mode.value === 'translation';
    captions.classList.toggle('translation-only', only);
    renderEmpty();
    if (only && selection?.field === 'text') selectCaption(null);
    renderSpeech();
    if (follow) scrollToLatest();
  }
  function renderEmpty() {
    const only = mode.value === 'translation';
    empty.hidden = only ? captions.querySelector('.has-translation') !== null : rows.size > 0;
    empty.textContent = only ? '等待译文 · 请在主窗口选择目标语言' : '等待实时字幕';
  }
  function renderUpdate(update) {
    const follow = update.reset || atBottom();
    if (update.reset) {
      selectCaption(null);
      rows.clear();
      captions.replaceChildren();
    }
    if (update.overlay_open === false && selection) selectCaption(null);
    for (const row of update.transcripts) {
      let entry = rows.get(row.id);
      if (!entry) {
        const article = document.createElement('article');
        article.className = 'caption';
        const original = document.createElement('div');
        original.className = 'original';
        const translation = document.createElement('div');
        translation.className = 'translation';
        article.append(original, translation);
        captions.append(article);
        entry = {article, original, translation};
        rows.set(row.id, entry);
      }
      renderSegments(entry.original, row, 'text');
      renderSegments(entry.translation, row, 'translation');
      const hasTranslation = entry.translation.childElementCount > 0;
      entry.article.classList.toggle('has-translation', hasTranslation);
      entry.article.classList.toggle('no-translation', !hasTranslation);
    }
    if (update.reset || update.transcripts.length) {
      renderEmpty();
      if (follow) scrollToLatest();
    }
    toggle.textContent = {
      idle: '开始监听', starting: '取消连接', live: '停止监听', stopping: '正在停止',
    }[update.phase] || '开始监听';
    toggle.disabled = !update.phase || update.phase === 'stopping';
  }
  function renderSegments(container, row, field) {
    const lines = (row[`${field}_lines`] ?? (row[field] || '').split('\n'))
      .map((line) => line.trim()).filter(Boolean);
    while (container.childElementCount > lines.length) container.lastElementChild.remove();
    for (const [index, text] of lines.entries()) {
      let button = container.children[index];
      if (!button) {
        button = document.createElement('button');
        button.type = 'button';
        button.className = 'caption-segment';
        button.setAttribute('aria-pressed', 'false');
        button.onclick = () => selectCaption({id: row.id, field, button, text: button.textContent});
        container.append(button);
      }
      if (button.textContent !== text) button.textContent = text;
    }
    if (selection?.id === row.id && selection.field === field &&
        (!container.contains(selection.button) || selection.text !== selection.button.textContent)) {
      selectCaption(null);
    }
  }
  mode.onchange = async () => {
    renderMode();
    if (!await api.set_overlay_mode(mode.value)) mode.value = await api.overlay_mode();
    renderMode();
  };
  toggle.onclick = () => api.overlay_toggle_capture();
  resize.onpointerdown = (event) => {
    drag = {x: event.screenX, y: event.screenY, w: innerWidth, h: innerHeight};
    resize.setPointerCapture(event.pointerId);
  };
  resize.onpointermove = (event) => {
    if (drag) api.resize_overlay(drag.w + event.screenX - drag.x, drag.h + event.screenY - drag.y);
  };
  resize.onlostpointercapture = () => { drag = null; };
  async function refresh() {
    try {
      const update = await api.overlay_caption(revision, session);
      revision = update.revision;
      session = update.session;
      renderUpdate(update);
      speech = await api.caption_speech_status();
      renderSpeech();
    } finally {
      window.setTimeout(refresh, 180);
    }
  }
  renderMode();
  applyFont();
  refresh();
});
