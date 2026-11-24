import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:vtoh/main.dart';

void main() {
  testWidgets('narrow layout switches between subtitles and answers', (
    tester,
  ) async {
    tester.view.devicePixelRatio = 1;
    tester.view.physicalSize = const Size(360, 800);
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    await tester.pumpWidget(const VtoHApp());
    expect(find.text('开始监听'), findsOneWidget);
    expect(find.text('连接设置'), findsNothing);
    expect(find.byType(TextField), findsNothing);
    expect(find.text('开始监听后，字幕会显示在这里。'), findsOneWidget);
    expect(find.text('等待完整的问题。'), findsNothing);

    final answers = find.text('问答');
    await tester.ensureVisible(answers);
    await tester.tap(answers);
    await tester.pumpAndSettle();

    expect(find.text('等待完整的问题。'), findsOneWidget);
    expect(find.text('开始监听后，字幕会显示在这里。'), findsNothing);
    await tester.tap(find.text('翻译'));
    await tester.pumpAndSettle();
    expect(find.text('选择目标语言后开始监听。'), findsOneWidget);

    await tester.tap(find.byTooltip('设置'));
    await tester.pumpAndSettle();
    expect(find.text('连接设置'), findsOneWidget);
    expect(find.text('服务地址'), findsOneWidget);
    expect(find.text('访问密钥'), findsOneWidget);
    expect(find.text('开始监听'), findsNothing);
    expect(tester.takeException(), isNull);
  });
}
