'use strict';

const byId = (id) => document.getElementById(id);
const transcripts = new Map();
const answers = new Map();
const selected = new Set();
let bilingual = false;
let phase = 'idle';
let startPending = false;
let pairUrl = '';
let pairImage = '';
let hasSession = false;
let lastSound = Date.now();
let noticeIsSilence = false;
let api;

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text) element.textContent = text;
  if (className) element.className = className;
  return element;
}

function notice(text, error = false) {
  noticeIsSilence = false;
  byId('notice').textContent = text;
  byId('notice').toggleAttribute('data-error', error);
}

function updateControls() {
  const active = phase !== 'idle';
  byId('capture').disabled = startPending || phase === 'stopping' ||
    (phase === 'idle' && !byId('source').selectedOptions.length);
  byId('capture').toggleAttribute('data-stop', active);
  byId('capture-label').textContent = {
    idle: '开始监听', starting: '取消连接', live: '停止监听', stopping: '正在停止',
  }[phase];
  for (const id of ['source', 'language', 'refresh', 'settings']) {
    byId(id).disabled = active;
  }
  byId('pair').disabled = !pairImage;
  byId('answer').disabled = !hasSession || !selected.size || bilingual;
  byId('answer').textContent = selected.size ? `回答这段（${selected.size}）` : '回答这段';
  const state = byId('state');
  state.toggleAttribute('data-active', phase === 'live');
  state.textContent = {idle: '待机', starting: '正在连接', live: '监听中', stopping: '正在停止'}[phase];
}

function renderTranscript(row) {
  const entry = node('article', '', `entry${row.final ? '' : ' partial'}`);
  entry.id = `transcript-${row.id}`;
  const line = node('label', '', 'line');
  if (row.final && !bilingual) {
    const checkbox = node('input');
    checkbox.type = 'checkbox';
    checkbox.checked = selected.has(row.id);
    checkbox.addEventListener('change', () => {
      if (checkbox.checked && selected.size >= 8) {
        checkbox.checked = false;
        notice('一次最多选择 8 条字幕。');
      } else if (checkbox.checked) {
        selected.add(row.id);
      } else {
        selected.delete(row.id);
      }
      updateControls();
    });
    line.append(checkbox);
  }
  line.append(node('span', row.text_lines?.join('\n') ?? row.text, 'caption-text'));
  entry.append(line);
  if (bilingual && row.translation_status) {
    const fallback = row.translation_status === 'pending' ? '正在翻译…' : '暂无法翻译';
    const translation = node('p', row.translation_lines?.join('\n') || row.translation || fallback, 'translation');
    if (row.translation) translation.lang = row.translation_language;
    entry.append(translation);
  }
  return entry;
}

function renderTranscripts() {
  const target = byId('transcripts');
  const rows = [...transcripts.values()].filter((row) => !bilingual || row.translation_status);
  target.replaceChildren(...(rows.length ? rows.map(renderTranscript) : [
    node('p', bilingual ? '选择目标语言后开始监听。' : '开始监听后，字幕会显示在这里。', 'empty'),
  ]));
  target.scrollTop = target.scrollHeight;
}

function updateTranscript(row) {
  transcripts.set(row.id, row);
  if (transcripts.size > 250) {
    const first = transcripts.keys().next().value;
    transcripts.delete(first);
    selected.delete(first);
    byId(`transcript-${first}`)?.remove();
  }
  if (bilingual && !row.translation_status) return;
  const stream = byId('transcripts');
  const atBottom = stream.scrollHeight - stream.clientHeight - stream.scrollTop < 48;
  const existing = byId(`transcript-${row.id}`);
  if (existing) {
    if (!row.final || bilingual || !existing.querySelector('input')) {
      existing.replaceWith(renderTranscript(row));
    }
  } else {
    stream.querySelector('.empty')?.remove();
    stream.append(renderTranscript(row));
  }
  if (atBottom) stream.scrollTop = stream.scrollHeight;
  updateControls();
}

function renderAnswers(openAnswer, collapseAnswer) {
  const stream = byId('answers');
  const expanded = new Set([...stream.querySelectorAll('details[open]')].map((item) => item.dataset.id));
  expanded.delete(collapseAnswer);
  const initialize = !stream.querySelector('details');
  const cards = [...answers.values()].reverse().map((answer, index) => {
    const card = node('details', '', 'answer-card');
    card.dataset.id = answer.id;
    card.open = expanded.has(answer.id) || answer.id === openAnswer || (initialize && index === 0);
    const body = node('div', '', 'answer-body');
    body.append(node('p', answer.conclusion, 'answer-text'));
    if (answer.points.length) {
      const list = node('ul');
      list.append(...answer.points.map((point) => node('li', point)));
      body.append(list);
    }
    const copy = node('button', '复制答案', 'quiet answer-copy');
    copy.onclick = async () => {
      try {
        await navigator.clipboard.writeText([answer.conclusion, ...answer.points.map((point) => `• ${point}`)].join('\n'));
        copy.textContent = '已复制';
      } catch {
        copy.textContent = '复制失败';
      }
    };
    body.append(copy);
    card.append(node('summary', answer.question), body);
    return card;
  });
  stream.replaceChildren(...(cards.length ? cards : [
    node('p', '识别到完整问题后，答案会显示在这里。', 'empty'),
  ]));
}

function switchView(value) {
  bilingual = value;
  byId('originals').setAttribute('aria-pressed', String(!value));
  byId('bilingual').setAttribute('aria-pressed', String(value));
  renderTranscripts();
  updateControls();
}

async function refreshSources() {
  const picker = byId('source');
  const previous = picker.value;
  const result = await api.sources();
  if (result.error) {
    notice(result.error, true);
    return;
  }
  picker.replaceChildren(...result.sources.map(({id, label}) => {
    const option = node('option', label);
    option.value = id;
    return option;
  }));
  picker.value = result.sources.some((source) => source.id === previous) ? previous : '0';
  updateControls();
}

async function openSettings() {
  const settings = await api.settings();
  byId('key').value = '';
  byId('key').placeholder = settings.has_key ? '已设置，留空保留现有密钥' : '';
  byId('settings-error').textContent = '';
  byId('settings-dialog').showModal();
}

function applyEvent(event) {
  switch (event.type) {
    case 'toggle_capture':
      if (!document.querySelector('dialog[open]')) byId('capture').click();
      return;
    case 'toggle_overlay':
      byId('overlay').click();
      return;
    case 'snapshot':
      transcripts.clear();
      answers.clear();
      for (const row of event.transcripts) transcripts.set(row.id, row);
      for (const answer of event.answers) answers.set(answer.id, answer);
      for (const id of selected) if (!transcripts.has(id)) selected.delete(id);
      hasSession = true;
      renderTranscripts();
      renderAnswers();
      break;
    case 'transcript':
      updateTranscript(event);
      return;
    case 'answer': {
      const isNew = !answers.has(event.id);
      const previous = [...answers.keys()].at(-1);
      answers.set(event.id, event);
      if (answers.size > 120) answers.delete(answers.keys().next().value);
      const showLatest = isNew && byId('answers').scrollTop < 24;
      renderAnswers(showLatest ? event.id : undefined, showLatest ? previous : undefined);
      return;
    }
    case 'pair':
      pairImage = event.image;
      pairUrl = event.url;
      break;
    case 'level':
      byId('signal').value = event.peak;
      if (event.peak > 200) {
        lastSound = Date.now();
        if (noticeIsSilence) notice('');
      } else if (phase === 'live' && Date.now() - lastSound > 4000 && !byId('notice').textContent) {
        notice('持续无声，请检查播放音源。');
        noticeIsSilence = true;
      }
      break;
    case 'error':
      notice(event.message, true);
      break;
    case 'status':
      notice(event.message);
      byId('signal').value = 0;
      break;
    case 'expired':
      notice('会话已过期，请重新开始监听。');
      break;
    case 'finished':
      hasSession = false;
      byId('signal').value = 0;
      break;
  }
  updateControls();
}

async function poll() {
  try {
    const state = await api.poll_workspace();
    phase = state.phase;
    byId('overlay').setAttribute('aria-pressed', String(state.overlay_open));
    for (const event of state.events) applyEvent(event);
    updateControls();
  } catch {
    notice('客户端通信异常，请重新打开 VtoH。', true);
  } finally {
    window.setTimeout(poll, 85);
  }
}

byId('originals').addEventListener('click', () => switchView(false));
byId('bilingual').addEventListener('click', () => switchView(true));
byId('refresh').addEventListener('click', refreshSources);
byId('settings').addEventListener('click', openSettings);
byId('overlay').addEventListener('click', async () => {
  const visible = await api.toggle_overlay();
  byId('overlay').setAttribute('aria-pressed', String(visible));
});
byId('cancel').addEventListener('click', () => byId('settings-dialog').close());
byId('connection-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const result = await api.save_settings(byId('key').value || null);
  if (result.error) {
    byId('settings-error').textContent = result.error;
  } else {
    byId('key').value = '';
    byId('settings-dialog').close();
    notice('');
  }
});
byId('pair').addEventListener('click', () => {
  byId('qr').src = pairImage;
  byId('pair-dialog').showModal();
});
byId('copy-pair').addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText(pairUrl);
    byId('copy-pair').textContent = '链接已复制';
  } catch {
    byId('copy-pair').textContent = '复制失败，请扫码';
  }
});
byId('answer').addEventListener('click', async () => {
  const result = await api.answer([...selected]);
  if (result.error) notice(result.error, true);
});
byId('answer-focus').addEventListener('click', () => {
  const expanded = byId('panes').classList.toggle('answer-focus');
  byId('captions-pane').hidden = expanded;
  byId('answer-focus').setAttribute('aria-pressed', String(expanded));
  byId('answer-focus').textContent = expanded ? '恢复双栏' : '展开问答';
});
byId('capture').addEventListener('click', async () => {
  if (phase === 'live' || phase === 'starting') {
    phase = await api.stop_capture();
    updateControls();
    return;
  }
  startPending = true;
  notice('');
  updateControls();
  lastSound = Date.now();
  const target = byId('language').value;
  try {
    const result = await api.start_capture(byId('source').value, target);
    if (result.started && target) switchView(true);
    if (result.settings_required) await openSettings();
    if (result.error) notice(result.error, true);
  } catch {
    notice('无法启动监听，请重试。', true);
  } finally {
    startPending = false;
    updateControls();
  }
});

renderTranscripts();
renderAnswers();
window.addEventListener('pywebviewready', async () => {
  api = window.pywebview.api;
  await refreshSources();
  poll();
});
