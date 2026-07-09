// mac-audio-capture.swift
// Captures system audio via ScreenCaptureKit and writes raw PCM (16kHz, mono, int16) to stdout.
// Usage: mac-audio-capture [--app BundleID] [--sample-rate 16000]
//   No args = capture all system audio
//   --app com.apple.FaceTime = capture only that app's audio

import Foundation
import ScreenCaptureKit
import AVFoundation
import CoreMedia

// MARK: - Configuration

struct CaptureConfig {
    var sampleRate: Int = 16000
    var appBundleID: String? = nil
}

func parseArgs() -> CaptureConfig {
    var config = CaptureConfig()
    let args = CommandLine.arguments
    var i = 1
    while i < args.count {
        switch args[i] {
        case "--app":
            i += 1
            if i < args.count { config.appBundleID = args[i] }
        case "--sample-rate":
            i += 1
            if i < args.count { config.sampleRate = Int(args[i]) ?? 16000 }
        default:
            break
        }
        i += 1
    }
    return config
}

// MARK: - Audio Capture Delegate

class AudioCaptureDelegate: NSObject, SCStreamOutput {
    let targetSampleRate: Int

    init(targetSampleRate: Int) {
        self.targetSampleRate = targetSampleRate
        super.init()
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .audio else { return }
        guard let blockBuffer = CMSampleBufferGetDataBuffer(sampleBuffer) else { return }

        var length = 0
        var dataPointer: UnsafeMutablePointer<Int8>?
        let status = CMBlockBufferGetDataPointer(blockBuffer, atOffset: 0, lengthAtOffsetOut: nil, totalLengthOut: &length, dataPointerOut: &dataPointer)
        guard status == kCMBlockBufferNoErr, let ptr = dataPointer, length > 0 else { return }

        // ScreenCaptureKit outputs Float32 samples. Convert to Int16 for ASR.
        let formatDesc = CMSampleBufferGetFormatDescription(sampleBuffer)
        guard let asbd = formatDesc.flatMap({ CMAudioFormatDescriptionGetStreamBasicDescription($0)?.pointee }) else { return }

        let sourceSampleRate = Int(asbd.mSampleRate)
        let sourceChannels = Int(asbd.mChannelsPerFrame)
        let float32Count = length / MemoryLayout<Float32>.size

        let float32Ptr = UnsafeRawPointer(ptr).bindMemory(to: Float32.self, capacity: float32Count)
        let float32Buffer = UnsafeBufferPointer(start: float32Ptr, count: float32Count)

        // Mix down to mono if needed
        let monoSamples: [Float32]
        if sourceChannels > 1 {
            let frameCount = float32Count / sourceChannels
            monoSamples = (0..<frameCount).map { frame in
                var sum: Float32 = 0
                for ch in 0..<sourceChannels {
                    sum += float32Buffer[frame * sourceChannels + ch]
                }
                return sum / Float32(sourceChannels)
            }
        } else {
            monoSamples = Array(float32Buffer)
        }

        // Resample if needed (simple linear interpolation)
        let resampled: [Float32]
        if sourceSampleRate != targetSampleRate {
            let ratio = Double(targetSampleRate) / Double(sourceSampleRate)
            let outputCount = Int(Double(monoSamples.count) * ratio)
            resampled = (0..<outputCount).map { i in
                let srcIndex = Double(i) / ratio
                let lo = Int(srcIndex)
                let hi = min(lo + 1, monoSamples.count - 1)
                let frac = Float32(srcIndex - Double(lo))
                return monoSamples[lo] * (1 - frac) + monoSamples[hi] * frac
            }
        } else {
            resampled = monoSamples
        }

        // Convert Float32 [-1.0, 1.0] to Int16
        let int16Samples = resampled.map { sample -> Int16 in
            let clamped = max(-1.0, min(1.0, sample))
            return Int16(clamped * Float32(Int16.max))
        }

        // Write to stdout
        int16Samples.withUnsafeBufferPointer { buffer in
            let rawPtr = UnsafeRawPointer(buffer.baseAddress!)
            let byteCount = buffer.count * MemoryLayout<Int16>.size
            let data = Data(bytes: rawPtr, count: byteCount)
            FileHandle.standardOutput.write(data)
        }
    }
}

// MARK: - Main

// Kept at file scope so ARC doesn't reclaim them after startCapture returns.
var _stream: AnyObject?
var _delegate: AnyObject?

@available(macOS 13.0, *)
func startCapture(config: CaptureConfig) async throws {
    let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)

    let filter: SCContentFilter
    if let bundleID = config.appBundleID {
        guard let app = content.applications.first(where: { $0.bundleIdentifier == bundleID }) else {
            FileHandle.standardError.write("App not found: \(bundleID)\n".data(using: .utf8)!)
            exit(1)
        }
        filter = SCContentFilter(desktopIndependentWindow: content.windows.first(where: { $0.owningApplication == app }) ?? content.windows[0])
    } else {
        guard let display = content.displays.first else {
            FileHandle.standardError.write("No display found\n".data(using: .utf8)!)
            exit(1)
        }
        filter = SCContentFilter(display: display, excludingApplications: [], exceptingWindows: [])
    }

    let streamConfig = SCStreamConfiguration()
    streamConfig.capturesAudio = true
    streamConfig.excludesCurrentProcessAudio = true
    streamConfig.width = 2
    streamConfig.height = 2
    streamConfig.minimumFrameInterval = CMTime(value: 1, timescale: 1)
    streamConfig.sampleRate = 48000
    streamConfig.channelCount = 2

    let stream = SCStream(filter: filter, configuration: streamConfig, delegate: nil)
    let delegate = AudioCaptureDelegate(targetSampleRate: config.sampleRate)

    try stream.addStreamOutput(delegate, type: .audio, sampleHandlerQueue: DispatchQueue(label: "audio-capture"))

    try await stream.startCapture()

    // Pin to globals so they survive the function return.
    _stream = stream
    _delegate = delegate

    FileHandle.standardError.write("Audio capture started (sample_rate=\(config.sampleRate))\n".data(using: .utf8)!)
}

if #available(macOS 13.0, *) {
    let config = parseArgs()
    signal(SIGINT) { _ in exit(0) }
    signal(SIGTERM) { _ in exit(0) }
    Task {
        do {
            try await startCapture(config: config)
        } catch {
            FileHandle.standardError.write("Error: \(error)\n".data(using: .utf8)!)
            exit(1)
        }
    }
    dispatchMain()
} else {
    FileHandle.standardError.write("Requires macOS 13.0+\n".data(using: .utf8)!)
    exit(1)
}
