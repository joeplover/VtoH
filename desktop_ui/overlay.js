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
  const rows = new Map();
  let revision = -1;
  let session = null;
  let drag;
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
      rows.clear();
      captions.replaceChildren();
    }
    for (const row of update.transcripts) {
      let entry = rows.get(row.id);
      if (!entry) {
        const article = document.createElement('article');
        article.className = 'caption';
        const original = document.createElement('p');
        original.className = 'original';
        const translation = document.createElement('p');
        translation.className = 'translation';
        article.append(original, translation);
        captions.append(article);
        entry = {article, original, translation};
        rows.set(row.id, entry);
      }
      entry.original.textContent = row.text;
      entry.translation.textContent = row.translation || '';
      entry.translation.hidden = !row.translation;
      entry.article.classList.toggle('has-translation', Boolean(row.translation));
      entry.article.classList.toggle('no-translation', !row.translation);
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
    } finally {
      window.setTimeout(refresh, 180);
    }
  }
  renderMode();
  applyFont();
  refresh();
});
