import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:mobile_scanner/mobile_scanner.dart';
import 'package:qr_flutter/qr_flutter.dart';

void main() => runApp(const VtoHApp());

class VtoHApp extends StatelessWidget {
  const VtoHApp({super.key});

  @override
  Widget build(BuildContext context) => MaterialApp(
    title: 'VtoH · 实时问答',
    debugShowCheckedModeBanner: false,
    theme: ThemeData(
      colorScheme: ColorScheme.fromSeed(seedColor: const Color(0xff246449)),
      scaffoldBackgroundColor: const Color(0xfff2f5f3),
      appBarTheme: const AppBarTheme(
        backgroundColor: Color(0xfff2f5f3),
        centerTitle: false,
      ),
      inputDecorationTheme: const InputDecorationTheme(
        border: OutlineInputBorder(),
      ),
      filledButtonTheme: FilledButtonThemeData(
        style: FilledButton.styleFrom(minimumSize: const Size(0, 48)),
      ),
    ),
    home: const Workspace(),
  );
}

typedef Event = Map<String, dynamic>;
const _serverPrefix = '/vtoh';
const _defaultServerUrl = 'https://www.ploverjoe.xyz/vtoh/';
const _translationOptions = {
  '': '关闭翻译',
  'zh': '简体中文',
  'en': '英语',
  'ja': '日语',
  'ko': '韩语',
  'fr': '法语',
  'es': '西班牙语',
  'de': '德语',
};

enum WorkspaceView { subtitles, translation, answers }

class Workspace extends StatefulWidget {
  const Workspace({super.key});

  @override
  State<Workspace> createState() => _WorkspaceState();
}

class _WorkspaceState extends State<Workspace> {
  static const _native = MethodChannel('vtoh/capture');
  static const _frames = EventChannel('vtoh/frames');
  final _server = TextEditingController(
    text: const String.fromEnvironment(
      'VTOH_SERVER_URL',
      defaultValue: _defaultServerUrl,
    ),
  );
  final _key = TextEditingController();
  final _transcripts = <String, Event>{};
  final _answers = <String, Event>{};
  final _selected = <String>{};
  final _sources = <String, String>{};
  String _source = 'mic';
  String _notice = '';
  bool _busy = false;
  bool _recording = false;
  bool _watching = false;
  WorkspaceView _view = WorkspaceView.subtitles;
  String _translationTarget = '';
  String? _sessionKey;
  bool _remoteActive = false;
  bool _expired = false;
  double _level = 0;
  DateTime _lastSound = DateTime.now();
  Uri? _base;
  String? _id;
  String? _owner;
  String? _viewer;
  WebSocket? _updates;
  WebSocket? _audio;
  StreamSubscription<dynamic>? _capture;
  Process? _helper;
  Completer<void>? _audioStopped;
  Timer? _retry;
  int _attempts = 0;
  int _generation = 0;

  String get _exe =>
      '${File(Platform.resolvedExecutable).parent.path}${Platform.pathSeparator}vtoh_capture.exe';

  @override
  void initState() {
    super.initState();
    if (Platform.isWindows) {
      _source = '0';
      _refresh();
    } else {
      _sources.addAll({'mic': '麦克风', 'playback': '应用播放声音'});
    }
  }

  Future<void> _refresh() async {
    try {
      final result = await Process.run(_exe, ['list'], stdoutEncoding: utf8);
      if (result.exitCode != 0) throw StateError('读取音源失败');
      if (!mounted) return;
      setState(() {
        _sources
          ..clear()
          ..addEntries(
            (result.stdout as String).trim().split('\n').map((line) {
              final fields = line.trim().split('\t');
              return MapEntry(
                fields.first,
                fields.first == '0'
                    ? '默认播放设备'
                    : '${fields.last} · ${fields.first}',
              );
            }),
          );
        if (!_sources.containsKey(_source)) _source = '0';
      });
    } catch (_) {
      _message('找不到 Windows 采集组件，请检查 vtoh_capture.exe。');
    }
  }

  void _message(String text) {
    if (mounted) setState(() => _notice = text);
  }

  Uri _serverUri(String raw) {
    final uri = Uri.parse(raw.trim().replaceFirst(RegExp(r'/+$'), ''));
    final path = uri.path.replaceFirst(RegExp(r'/+$'), '');
    if (!['https', 'http'].contains(uri.scheme) ||
        uri.host.isEmpty ||
        uri.hasFragment ||
        uri.hasQuery ||
        uri.userInfo.isNotEmpty ||
        (path.isNotEmpty && path != _serverPrefix)) {
      throw const FormatException('请输入服务地址，例如 $_defaultServerUrl');
    }
    return uri.replace(path: _serverPrefix);
  }

  Uri _ws(String path, String token, {String target = ''}) => _base!.replace(
    scheme: _base!.scheme == 'https' ? 'wss' : 'ws',
    path: '$_serverPrefix/sessions/$_id/$path',
    queryParameters: {
      'token': token,
      if (target.isNotEmpty) 'translate': target,
    },
  );

  Uri get _pairUri => _base!.replace(
    path: '$_serverPrefix/pair',
    fragment: 'id=$_id&token=$_viewer',
  );

  Future<void> _createSession(Uri base) async {
    final client = HttpClient()
      ..connectionTimeout = const Duration(seconds: 10);
    try {
      final request = await client.postUrl(
        base.replace(path: '$_serverPrefix/sessions'),
      );
      request.headers.set('Authorization', 'Bearer ${_key.text}');
      final response = await request.close().timeout(
        const Duration(seconds: 12),
      );
      final body = await response
          .transform(utf8.decoder)
          .join()
          .timeout(const Duration(seconds: 10));
      if (response.statusCode != 201) {
        throw StateError(response.statusCode == 401 ? '访问密钥有误' : '无法建立会话');
      }
      final data = jsonDecode(body) as Event;
      _base = base;
      _id = data['id'] as String;
      _owner = data['owner'] as String;
      _viewer = data['viewer'] as String;
      _sessionKey = _key.text;
    } finally {
      client.close(force: true);
    }
  }

  Future<void> _connectUpdates(int generation) async {
    if (generation != _generation || _expired) return;
    try {
      final socket = await WebSocket.connect(
        _ws(
          _watching ? 'watch' : 'control',
          (_watching ? _viewer : _owner)!,
        ).toString(),
      ).timeout(const Duration(seconds: 12));
      if (!mounted || generation != _generation) {
        await socket.close();
        return;
      }
      _updates = socket;
      socket.pingInterval = const Duration(seconds: 15);
      socket.listen(
        (raw) => _onEvent(jsonDecode(raw as String) as Event),
        onError: (Object _) {},
        onDone: () => _reconnect(generation),
        cancelOnError: true,
      );
    } catch (_) {
      _reconnect(generation);
    }
  }

  void _reconnect(int generation) {
    if (!mounted || generation != _generation || _expired) return;
    _updates = null;
    if (_attempts >= 8) {
      _message('连接失败，请重新加入会话或开始监听。');
      return;
    }
    _message('连接中断，正在重连');
    _retry?.cancel();
    _retry = Timer(
      Duration(seconds: 1 << _attempts.clamp(0, 3)),
      () => _connectUpdates(generation),
    );
    _attempts++;
  }

  void _onEvent(Event event) {
    if (!mounted) return;
    setState(() {
      switch (event['type']) {
        case 'snapshot':
          _attempts = 0;
          _notice = '';
          _remoteActive = event['recording'] as bool;
          _transcripts
            ..clear()
            ..addEntries(_indexed(event['transcripts'] as List<dynamic>));
          _answers
            ..clear()
            ..addEntries(_indexed(event['answers'] as List<dynamic>));
          _selected.retainAll(_transcripts.keys);
        case 'capture':
          _remoteActive = event['recording'] as bool;
        case 'transcript':
          _transcripts[event['id'] as String] = event;
          if (_transcripts.length > 250) {
            final first = _transcripts.keys.first;
            _transcripts.remove(first);
            _selected.remove(first);
          }
        case 'answer':
          _answers[event['id'] as String] = event;
          if (_answers.length > 120) _answers.remove(_answers.keys.first);
        case 'error':
          _notice = event['message'] as String;
        case 'expired':
          _expired = true;
          _notice = '会话已结束';
      }
    });
  }

  Iterable<MapEntry<String, Event>> _indexed(List<dynamic> rows) =>
      rows.cast<Event>().map((row) => MapEntry(row['id'] as String, row));

  Future<void> _resetUpdates() async {
    _generation++;
    _retry?.cancel();
    _attempts = 0;
    _expired = false;
    final socket = _updates;
    _updates = null;
    if (socket != null) await socket.close();
    _transcripts.clear();
    _answers.clear();
    _selected.clear();
  }

  Future<void> _join(String raw) async {
    try {
      final uri = Uri.parse(raw.trim());
      final values = Uri.splitQueryString(uri.fragment);
      final id = values['id'];
      final token = values['token'];
      if (uri.path != '$_serverPrefix/pair' ||
          id == null ||
          token == null ||
          !RegExp(r'^[a-f0-9]{32}$').hasMatch(id) ||
          !RegExp(r'^[A-Za-z0-9_-]{32}$').hasMatch(token)) {
        throw const FormatException('配对链接格式有误');
      }
      final base = _serverUri(uri.origin);
      await _resetUpdates();
      if (!mounted) return;
      setState(() {
        _base = base;
        _id = id;
        _viewer = token;
        _owner = null;
        _watching = true;
        _notice = '正在连接电脑会话';
      });
      await _connectUpdates(_generation);
    } catch (_) {
      _message('无法加入，请检查配对链接。');
    }
  }

  Future<void> _start() async {
    if (_key.text.isEmpty &&
        _serverUri(_defaultServerUrl).toString() ==
            _server.text.trim().replaceFirst(RegExp(r'/+$'), '')) {
      await _openSettings();
      return;
    }
    setState(() {
      _busy = true;
      _watching = false;
      _notice = '正在连接服务';
      if (_translationTarget.isNotEmpty) _view = WorkspaceView.translation;
    });
    try {
      final base = _serverUri(_server.text);
      if (_base != base ||
          _owner == null ||
          _sessionKey != _key.text ||
          _expired ||
          _attempts >= 8) {
        await _resetUpdates();
        await _createSession(base);
        await _connectUpdates(_generation);
      } else if (_updates == null) {
        await _connectUpdates(_generation);
      }
      if (!mounted) return;
      final audio = await WebSocket.connect(
        _ws('audio', _owner!, target: _translationTarget).toString(),
      ).timeout(const Duration(seconds: 12));
      if (!mounted) {
        await audio.close();
        return;
      }
      _audio = audio;
      final ready = Completer<void>();
      final stopped = Completer<void>();
      _audioStopped = stopped;
      audio.listen(
        (raw) {
          final event = jsonDecode(raw as String) as Event;
          if (event['type'] == 'ready' && !ready.isCompleted) ready.complete();
          if (event['type'] == 'stopped' && !stopped.isCompleted) {
            stopped.complete();
          }
        },
        onDone: () {
          if (!ready.isCompleted) ready.completeError(StateError('音频连接未就绪'));
          if (!stopped.isCompleted) stopped.complete();
          if (identical(_audio, audio) && _recording) {
            unawaited(_stop(message: '音频连接已断开，请重新开始监听。'));
          }
        },
        onError: (Object _) {},
        cancelOnError: true,
      );
      await ready.future.timeout(const Duration(seconds: 12));
      _lastSound = DateTime.now();
      if (Platform.isWindows) {
        final helper = await Process.start(_exe, ['capture', _source]);
        _helper = helper;
        helper.stderr.drain<void>();
        _capture = helper.stdout.listen(
          _sendFrame,
          onDone: () {
            if (_recording) unawaited(_stop(message: 'Windows 音源已退出'));
          },
        );
      } else {
        _capture = _frames.receiveBroadcastStream().listen((value) {
          if (value is Uint8List) _sendFrame(value);
          if (value is String) unawaited(_stop(message: value));
        }, onError: (Object _) => unawaited(_stop(message: '采集已中断')));
        await _native.invokeMethod<void>('start', _source);
      }
      if (audio.readyState != WebSocket.open) throw StateError('语音识别连接已关闭');
      if (!mounted) return;
      setState(() {
        _recording = true;
        _notice = '';
      });
    } catch (error) {
      await _stop(
        message: error is PlatformException
            ? (error.message ?? '采集授权失败')
            : error is FormatException
            ? error.message
            : error is StateError
            ? error.message
            : '启动监听失败，请检查连接和音源',
      );
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  void _sendFrame(List<int> data) {
    final socket = _audio;
    if (socket == null || socket.readyState != WebSocket.open) return;
    for (var offset = 0; offset < data.length; offset += 3200) {
      final end = (offset + 3200).clamp(0, data.length);
      socket.add(data.sublist(offset, end));
    }
    var peak = 0;
    final bytes = ByteData.sublistView(Uint8List.fromList(data));
    for (var i = 0; i + 1 < data.length; i += 2) {
      final value = bytes.getInt16(i, Endian.little).abs();
      if (value > peak) peak = value;
    }
    if (!mounted) return;
    final now = DateTime.now();
    if (peak > 200) _lastSound = now;
    setState(() => _level = (peak / 16000).clamp(0.0, 1.0));
  }

  Future<void> _stop({String message = ''}) async {
    final audio = _audio;
    _audio = null;
    if (mounted) {
      setState(() {
        _recording = false;
        _level = 0;
        _notice = message;
      });
    }
    final capture = _capture;
    _capture = null;
    final helper = _helper;
    _helper = null;
    final stopped = _audioStopped;
    _audioStopped = null;
    if (helper != null) {
      helper.stdin.write('stop');
      unawaited(helper.stdin.close().catchError((Object _) {}));
      unawaited(
        helper.exitCode.timeout(
          const Duration(seconds: 2),
          onTimeout: () {
            helper.kill();
            return -1;
          },
        ),
      );
    }
    if (Platform.isAndroid && capture != null) {
      try {
        await _native.invokeMethod<void>('stop');
      } on PlatformException {
        _message('采集服务已退出');
      }
    }
    await capture?.cancel();
    if (audio != null) {
      if (audio.readyState == WebSocket.open) {
        audio.add('{"type":"stop"}');
        await stopped?.future.timeout(
          const Duration(seconds: 8),
          onTimeout: () {},
        );
      }
      await audio.close();
    }
  }

  void _manual() {
    final socket = _updates;
    if (socket == null || socket.readyState != WebSocket.open) {
      _message('问答连接正在重连');
      return;
    }
    socket.add(jsonEncode({'type': 'answer', 'ids': _selected.toList()}));
    setState(_selected.clear);
  }

  Future<void> _scan() async {
    final link = await Navigator.of(
      context,
    ).push<String>(MaterialPageRoute(builder: (_) => const PairScanner()));
    if (link != null) await _join(link);
  }

  Future<void> _paste() async {
    final controller = TextEditingController();
    final link = await showDialog<String>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('输入配对链接'),
        content: TextField(controller: controller, autofocus: true),
        actions: [
          FilledButton(
            onPressed: () => Navigator.pop(context, controller.text),
            child: const Text('加入'),
          ),
        ],
      ),
    );
    controller.dispose();
    if (link != null) await _join(link);
  }

  void _showPair() {
    final link = _pairUri.toString();
    showDialog<void>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('手机配对'),
        content: SizedBox(
          width: 250,
          height: 250,
          child: QrImageView(data: link),
        ),
        actions: [
          TextButton(
            onPressed: () => Clipboard.setData(ClipboardData(text: link)),
            child: const Text('复制链接'),
          ),
        ],
      ),
    );
  }

  @override
  void dispose() {
    _generation++;
    _retry?.cancel();
    unawaited(_capture?.cancel());
    unawaited(_updates?.close());
    unawaited(_audio?.close());
    if (Platform.isAndroid && _recording) {
      unawaited(_native.invokeMethod<void>('stop'));
    }
    _helper?.kill();
    _server.dispose();
    _key.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final picking = !_recording && !_busy;
    final silence =
        _recording && DateTime.now().difference(_lastSound).inSeconds >= 4;
    final state = _watching
        ? (_remoteActive ? '电脑正在监听' : '电脑会话')
        : _busy
        ? (_recording ? '正在停止' : '正在处理')
        : _recording
        ? '正在监听'
        : '待机';
    return Scaffold(
      appBar: AppBar(
        title: const Text('VtoH'),
        actions: [
          if (_owner != null && !_expired)
            IconButton(
              tooltip: '手机配对',
              onPressed: _showPair,
              icon: const Icon(Icons.qr_code),
            ),
          if (Platform.isAndroid && picking) _pairMenu(),
          IconButton(
            tooltip: '设置',
            onPressed: picking ? _openSettings : null,
            icon: const Icon(Icons.settings_outlined),
          ),
          const SizedBox(width: 8),
        ],
      ),
      body: SafeArea(
        child: Center(
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 1100),
            child: ListView(
              padding: const EdgeInsets.fromLTRB(20, 10, 20, 24),
              children: [
                _capturePanel(state, picking),
                if (_notice.isNotEmpty || silence)
                  Padding(
                    padding: const EdgeInsets.only(top: 10),
                    child: Semantics(
                      liveRegion: true,
                      child: Text(
                        _notice.isNotEmpty ? _notice : '持续无声，请检查音源。',
                        style: TextStyle(
                          color: Theme.of(context).colorScheme.primary,
                        ),
                      ),
                    ),
                  ),
                const SizedBox(height: 18),
                LayoutBuilder(
                  builder: (context, bounds) {
                    final wide = bounds.maxWidth >= 720;
                    final view = wide && _view == WorkspaceView.answers
                        ? WorkspaceView.subtitles
                        : _view;
                    final content = Column(
                      crossAxisAlignment: CrossAxisAlignment.stretch,
                      children: [
                        SegmentedButton<WorkspaceView>(
                          showSelectedIcon: false,
                          segments: [
                            const ButtonSegment(
                              value: WorkspaceView.subtitles,
                              label: Text('字幕'),
                            ),
                            const ButtonSegment(
                              value: WorkspaceView.translation,
                              label: Text('翻译'),
                            ),
                            if (!wide)
                              const ButtonSegment(
                                value: WorkspaceView.answers,
                                label: Text('问答'),
                              ),
                          ],
                          selected: {view},
                          onSelectionChanged: (values) =>
                              setState(() => _view = values.first),
                        ),
                        const SizedBox(height: 12),
                        switch (view) {
                          WorkspaceView.subtitles => _subtitlePanel(
                            showTitle: false,
                          ),
                          WorkspaceView.translation => _translationPanel(),
                          WorkspaceView.answers => _answerPanel(
                            showTitle: false,
                          ),
                        },
                      ],
                    );
                    if (!wide) return content;
                    return Row(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Expanded(flex: 6, child: content),
                        const SizedBox(width: 18),
                        Expanded(flex: 5, child: _answerPanel()),
                      ],
                    );
                  },
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }

  Future<void> _openSettings() async {
    final result = await Navigator.of(context).push<(String, String)>(
      MaterialPageRoute(
        builder: (_) => ConnectionSettings(
          address: _server.text,
          keyValue: _key.text,
          validate: _serverUri,
        ),
      ),
    );
    if (result == null || !mounted) return;
    setState(() {
      _server.text = result.$1;
      _key.text = result.$2;
      _notice = '';
    });
  }

  Widget _pairMenu() => PopupMenuButton<String>(
    tooltip: '电脑会话',
    icon: const Icon(Icons.devices),
    onSelected: (action) async {
      if (action == 'scan') await _scan();
      if (action == 'paste') await _paste();
      if (action == 'leave') {
        await _resetUpdates();
        if (mounted) {
          setState(() {
            _watching = false;
            _remoteActive = false;
            _notice = '';
          });
        }
      }
    },
    itemBuilder: (_) => [
      const PopupMenuItem(value: 'scan', child: Text('扫码查看电脑问答')),
      const PopupMenuItem(value: 'paste', child: Text('输入配对链接')),
      if (_watching) const PopupMenuItem(value: 'leave', child: Text('退出电脑会话')),
    ],
  );

  Widget _capturePanel(String state, bool picking) {
    final colors = Theme.of(context).colorScheme;
    return _surface(
      children: [
        Align(
          alignment: Alignment.centerLeft,
          child: Chip(
            avatar: Icon(
              _watching
                  ? Icons.devices
                  : _recording
                  ? Icons.graphic_eq
                  : Icons.circle_outlined,
              size: 17,
            ),
            label: Text(state),
            side: BorderSide.none,
            backgroundColor: _recording || _remoteActive
                ? colors.primaryContainer
                : colors.surfaceContainer,
          ),
        ),
        if (!_watching) ...[
          const SizedBox(height: 10),
          Row(
            children: [
              Expanded(
                child: DropdownButtonFormField<String>(
                  key: ValueKey(_source),
                  initialValue: _sources.containsKey(_source) ? _source : null,
                  isExpanded: true,
                  decoration: const InputDecoration(labelText: '音源'),
                  items: _sources.entries
                      .map(
                        (entry) => DropdownMenuItem(
                          value: entry.key,
                          child: Text(
                            entry.value,
                            overflow: TextOverflow.ellipsis,
                          ),
                        ),
                      )
                      .toList(),
                  onChanged: picking
                      ? (value) => setState(() => _source = value!)
                      : null,
                ),
              ),
              if (Platform.isWindows)
                IconButton(
                  onPressed: picking ? _refresh : null,
                  tooltip: '刷新音源',
                  icon: const Icon(Icons.refresh),
                ),
              const SizedBox(width: 10),
              Expanded(
                child: DropdownButtonFormField<String>(
                  initialValue: _translationTarget,
                  isExpanded: true,
                  decoration: const InputDecoration(labelText: '翻译为'),
                  items: _translationOptions.entries
                      .map(
                        (entry) => DropdownMenuItem(
                          value: entry.key,
                          child: Text(
                            entry.value,
                            overflow: TextOverflow.ellipsis,
                          ),
                        ),
                      )
                      .toList(),
                  onChanged: picking
                      ? (value) {
                          if (value == null) return;
                          setState(() => _translationTarget = value);
                        }
                      : null,
                ),
              ),
            ],
          ),
          const SizedBox(height: 12),
          FilledButton.icon(
            onPressed: _busy
                ? null
                : _recording
                ? _stopFromPage
                : _start,
            icon: Icon(
              _recording ? Icons.stop_rounded : Icons.play_arrow_rounded,
            ),
            label: Text(_recording ? '停止监听' : '开始监听'),
          ),
          if (_recording) ...[
            const SizedBox(height: 10),
            Text('输入电平', style: Theme.of(context).textTheme.labelMedium),
            const SizedBox(height: 5),
            LinearProgressIndicator(
              value: _level,
              borderRadius: BorderRadius.circular(9),
              semanticsLabel: '输入电平',
            ),
          ],
        ],
      ],
    );
  }

  Future<void> _stopFromPage() async {
    setState(() => _busy = true);
    try {
      await _stop();
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Widget _surface({required List<Widget> children}) => DecoratedBox(
    decoration: BoxDecoration(
      color: Theme.of(context).colorScheme.surface,
      border: Border.all(color: Theme.of(context).colorScheme.outlineVariant),
      borderRadius: BorderRadius.circular(16),
    ),
    child: Padding(
      padding: const EdgeInsets.all(16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: children,
      ),
    ),
  );

  Widget _subtitlePanel({bool showTitle = true}) => _surface(
    children: [
      if (showTitle || (!_watching && _selected.isNotEmpty))
        Row(
          children: [
            Expanded(
              child: showTitle
                  ? Text('实时字幕', style: Theme.of(context).textTheme.titleLarge)
                  : const SizedBox.shrink(),
            ),
            if (!_watching)
              TextButton(
                onPressed: _selected.isEmpty ? null : _manual,
                child: Text(
                  _selected.isEmpty ? '回答这段' : '回答这段（${_selected.length}）',
                ),
              ),
          ],
        ),
      if (_transcripts.isEmpty)
        _empty(
          Icons.subtitles_outlined,
          _watching ? '等待电脑转写。' : '开始监听后，字幕会显示在这里。',
        ),
      for (final item in _transcripts.values.toList().reversed) ...[
        if (_watching || item['final'] != true)
          ListTile(
            contentPadding: EdgeInsets.zero,
            title: Text(item['text'] as String),
            subtitle: item['final'] == true ? null : const Text('转写中'),
          )
        else
          CheckboxListTile(
            contentPadding: EdgeInsets.zero,
            controlAffinity: ListTileControlAffinity.leading,
            title: Text(item['text'] as String),
            value: _selected.contains(item['id']),
            onChanged: (selected) => setState(() {
              final id = item['id'] as String;
              if (selected == false) {
                _selected.remove(id);
              } else if (_selected.length < 8) {
                _selected.add(id);
              }
            }),
          ),
        const Divider(height: 1),
      ],
    ],
  );

  Widget _answerPanel({bool showTitle = true}) => _surface(
    children: [
      if (showTitle) Text('问答', style: Theme.of(context).textTheme.titleLarge),
      if (_answers.isEmpty) _empty(Icons.question_answer_outlined, '等待完整的问题。'),
      for (final answer in _answers.values.toList().reversed)
        Padding(
          padding: const EdgeInsets.symmetric(vertical: 10),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                answer['question'] as String,
                style: Theme.of(
                  context,
                ).textTheme.titleMedium?.copyWith(fontWeight: FontWeight.bold),
              ),
              const SizedBox(height: 8),
              SelectableText(answer['conclusion'] as String),
              for (final point in (answer['points'] as List<dynamic>))
                Padding(
                  padding: const EdgeInsets.only(top: 8),
                  child: Row(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      const Padding(
                        padding: EdgeInsets.only(right: 8),
                        child: Text('•'),
                      ),
                      Expanded(child: SelectableText('$point')),
                    ],
                  ),
                ),
              const SizedBox(height: 12),
              const Divider(height: 1),
            ],
          ),
        ),
    ],
  );

  Widget _translationPanel() {
    final rows = _transcripts.values
        .where((row) => row['translation_status'] != null)
        .toList()
        .reversed;
    final theme = Theme.of(context);
    return _surface(
      children: [
        if (rows.isEmpty)
          _empty(
            Icons.translate,
            _watching
                ? '等待电脑的译文。'
                : _translationTarget.isEmpty
                ? '选择目标语言后开始监听。'
                : '每句字幕定稿后显示译文。',
          ),
        for (final row in rows) ...[
          Text(
            _translationOptions[row['translation_language']] ?? '',
            style: theme.textTheme.labelMedium?.copyWith(
              color: theme.colorScheme.primary,
            ),
          ),
          const SizedBox(height: 6),
          SelectableText(
            row['translation'] as String? ??
                (row['translation_status'] == 'pending' ? '正在翻译…' : '暂无法翻译'),
            style: theme.textTheme.titleMedium,
          ),
          const SizedBox(height: 6),
          SelectableText(
            row['text'] as String,
            style: theme.textTheme.bodyMedium?.copyWith(
              color: theme.colorScheme.onSurfaceVariant,
            ),
          ),
          const Divider(height: 26),
        ],
      ],
    );
  }

  Widget _empty(IconData icon, String message) => Padding(
    padding: const EdgeInsets.symmetric(vertical: 24),
    child: Column(
      children: [
        Icon(icon, color: Theme.of(context).colorScheme.primary),
        const SizedBox(height: 8),
        Text(message, textAlign: TextAlign.center),
      ],
    ),
  );
}

class ConnectionSettings extends StatefulWidget {
  const ConnectionSettings({
    super.key,
    required this.address,
    required this.keyValue,
    required this.validate,
  });

  final String address;
  final String keyValue;
  final Uri Function(String) validate;

  @override
  State<ConnectionSettings> createState() => _ConnectionSettingsState();
}

class _ConnectionSettingsState extends State<ConnectionSettings> {
  late final _address = TextEditingController(text: widget.address);
  late final _key = TextEditingController(text: widget.keyValue);
  bool _showKey = false;
  String? _error;

  void _save() {
    try {
      final address = widget.validate(_address.text);
      Navigator.pop(context, ('$address/', _key.text.trim()));
    } on FormatException catch (error) {
      setState(() => _error = error.message);
    }
  }

  @override
  void dispose() {
    _address.dispose();
    _key.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(title: const Text('连接设置')),
    body: SafeArea(
      child: Align(
        alignment: Alignment.topCenter,
        child: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 520),
          child: ListView(
            padding: const EdgeInsets.all(20),
            children: [
              TextField(
                controller: _address,
                keyboardType: TextInputType.url,
                decoration: InputDecoration(
                  labelText: '服务地址',
                  errorText: _error,
                ),
              ),
              const SizedBox(height: 16),
              TextField(
                controller: _key,
                obscureText: !_showKey,
                autocorrect: false,
                enableSuggestions: false,
                decoration: InputDecoration(
                  labelText: '访问密钥',
                  suffixIcon: IconButton(
                    tooltip: _showKey ? '隐藏密钥' : '显示密钥',
                    onPressed: () => setState(() => _showKey = !_showKey),
                    icon: Icon(
                      _showKey
                          ? Icons.visibility_off_outlined
                          : Icons.visibility_outlined,
                    ),
                  ),
                ),
              ),
              const SizedBox(height: 20),
              FilledButton(onPressed: _save, child: const Text('保存')),
            ],
          ),
        ),
      ),
    ),
  );
}

class PairScanner extends StatefulWidget {
  const PairScanner({super.key});

  @override
  State<PairScanner> createState() => _PairScannerState();
}

class _PairScannerState extends State<PairScanner> {
  bool _read = false;

  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(title: const Text('扫描电脑配对码')),
    body: MobileScanner(
      onDetect: (codes) {
        if (_read) return;
        for (final code in codes.barcodes) {
          if (code.rawValue == null) continue;
          _read = true;
          Navigator.pop(context, code.rawValue);
          break;
        }
      },
    ),
  );
}
