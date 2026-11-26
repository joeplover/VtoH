import AVFoundation
import Flutter
import Security
import UIKit

@main
@objc class AppDelegate: FlutterAppDelegate {
  private let microphone = MicrophoneCapture()

  override func application(
    _ application: UIApplication,
    didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]?
  ) -> Bool {
    GeneratedPluginRegistrant.register(with: self)
    let launched = super.application(application, didFinishLaunchingWithOptions: launchOptions)
    if let controller = window?.rootViewController as? FlutterViewController {
      let messenger = controller.binaryMessenger
      FlutterEventChannel(name: "vtoh/frames", binaryMessenger: messenger)
        .setStreamHandler(microphone)
      FlutterMethodChannel(name: "vtoh/capture", binaryMessenger: messenger)
        .setMethodCallHandler { [weak self] call, result in
          guard let microphone = self?.microphone else {
            result(FlutterError(code: "capture", message: "采集服务不可用。", details: nil))
            return
          }
          switch call.method {
          case "start":
            guard call.arguments as? String == "mic" else {
              result(FlutterError(code: "source", message: "请选择麦克风。", details: nil))
              return
            }
            microphone.start(result)
          case "stop":
            microphone.stop()
            result(nil)
          default:
            result(FlutterMethodNotImplemented)
          }
        }
      FlutterMethodChannel(name: "vtoh/workspace", binaryMessenger: messenger)
        .setMethodCallHandler(ConnectionKey.handle)
    }
    return launched
  }

  override func applicationDidEnterBackground(_ application: UIApplication) {
    microphone.interrupt("已切到后台，麦克风监听已停止。")
    super.applicationDidEnterBackground(application)
  }
}

private final class MicrophoneCapture: NSObject, FlutterStreamHandler {
  private let session = AVAudioSession.sharedInstance()
  private var engine: AVAudioEngine?
  private var sink: FlutterEventSink?
  private var pending: FlutterResult?
  private var generation = 0

  override init() {
    super.init()
    NotificationCenter.default.addObserver(
      self, selector: #selector(handleInterruption), name: AVAudioSession.interruptionNotification,
      object: session
    )
  }

  func onListen(withArguments arguments: Any?, eventSink events: @escaping FlutterEventSink)
    -> FlutterError?
  {
    sink = events
    return nil
  }

  func onCancel(withArguments arguments: Any?) -> FlutterError? {
    sink = nil
    stop()
    return nil
  }

  func start(_ result: @escaping FlutterResult) {
    guard pending == nil, engine == nil else {
      result(FlutterError(code: "busy", message: "麦克风正在启动或监听。", details: nil))
      return
    }
    pending = result
    generation += 1
    let requestedGeneration = generation
    session.requestRecordPermission { [weak self] allowed in
      DispatchQueue.main.async {
        guard let self, self.generation == requestedGeneration, let pending = self.pending else {
          return
        }
        self.pending = nil
        guard allowed else {
          pending(FlutterError(code: "permission", message: "请在 iPhone 设置中允许 VtoH 使用麦克风。", details: nil))
          return
        }
        do {
          try self.begin(requestedGeneration)
          pending(nil)
        } catch {
          self.stop()
          pending(FlutterError(code: "microphone", message: "无法启动 iPhone 麦克风。", details: nil))
        }
      }
    }
  }

  private func begin(_ activeGeneration: Int) throws {
    try session.setCategory(.record, mode: .measurement)
    try session.setActive(true)
    let audio = AVAudioEngine()
    let input = audio.inputNode
    let source = input.outputFormat(forBus: 0)
    guard source.sampleRate > 0, source.channelCount > 0,
      let target = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16_000,
                                channels: 1, interleaved: true),
      let converter = AVAudioConverter(from: source, to: target)
    else {
      throw NSError(domain: "VtoH.Microphone", code: 1)
    }
    input.installTap(onBus: 0, bufferSize: 4096, format: source) { [weak self] buffer, _ in
      let capacity = AVAudioFrameCount(ceil(Double(buffer.frameLength) * 16_000 / source.sampleRate)) + 32
      guard let pcm = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: capacity) else { return }
      var supplied = false
      var conversionError: NSError?
      let status = converter.convert(to: pcm, error: &conversionError) { _, inputStatus in
        inputStatus.pointee = supplied ? .noDataNow : .haveData
        defer { supplied = true }
        return supplied ? nil : buffer
      }
      guard status != .error, pcm.frameLength > 0, let samples = pcm.int16ChannelData else { return }
      let frame = Data(bytes: samples[0], count: Int(pcm.frameLength) * MemoryLayout<Int16>.size)
      DispatchQueue.main.async {
        guard let self, self.generation == activeGeneration, self.engine != nil else { return }
        self.sink?(FlutterStandardTypedData(bytes: frame))
      }
    }
    engine = audio
    audio.prepare()
    try audio.start()
  }

  func stop() {
    generation += 1
    if let pending {
      self.pending = nil
      pending(FlutterError(code: "cancelled", message: "监听已取消。", details: nil))
    }
    if let engine {
      engine.stop()
      engine.inputNode.removeTap(onBus: 0)
      self.engine = nil
    }
    try? session.setActive(false, options: .notifyOthersOnDeactivation)
  }

  func interrupt(_ message: String) {
    guard engine != nil || pending != nil else { return }
    stop()
    sink?(message)
  }

  @objc private func handleInterruption(_ notification: Notification) {
    guard let type = notification.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt,
      type == AVAudioSession.InterruptionType.began.rawValue else { return }
    DispatchQueue.main.async { [weak self] in
      self?.interrupt("系统已中断麦克风监听。")
    }
  }
}

private enum ConnectionKey {
  private static let query: [String: Any] = [
    kSecClass as String: kSecClassGenericPassword,
    kSecAttrService as String: "xyz.ploverjoe.vtoh",
    kSecAttrAccount as String: "access-key",
  ]

  static func handle(_ call: FlutterMethodCall, result: @escaping FlutterResult) {
    switch call.method {
    case "loadConnection":
      var request = query
      request[kSecReturnData as String] = true
      request[kSecMatchLimit as String] = kSecMatchLimitOne
      var value: CFTypeRef?
      let status = SecItemCopyMatching(request as CFDictionary, &value)
      if status == errSecItemNotFound {
        result(nil)
      } else if status == errSecSuccess, let data = value as? Data,
        let key = String(data: data, encoding: .utf8)
      {
        result(["key": key])
      } else {
        fail(result)
      }
    case "saveConnection":
      guard let args = call.arguments as? [String: Any], let key = args["key"] as? String else {
        fail(result)
        return
      }
      let status: OSStatus
      if key.isEmpty {
        status = SecItemDelete(query as CFDictionary)
      } else {
        let data = Data(key.utf8)
        let update = [kSecValueData as String: data]
        let existing = SecItemUpdate(query as CFDictionary, update as CFDictionary)
        if existing == errSecItemNotFound {
          var item = query
          item[kSecValueData as String] = data
          item[kSecAttrAccessible as String] = kSecAttrAccessibleWhenUnlockedThisDeviceOnly
          status = SecItemAdd(item as CFDictionary, nil)
        } else {
          status = existing
        }
      }
      if status == errSecSuccess || (key.isEmpty && status == errSecItemNotFound) {
        result(nil)
      } else {
        fail(result)
      }
    default:
      result(FlutterMethodNotImplemented)
    }
  }

  private static func fail(_ result: FlutterResult) {
    result(FlutterError(code: "keychain", message: "无法访问 iPhone 密钥存储。", details: nil))
  }
}
