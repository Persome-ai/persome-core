import CoreGraphics
import Foundation
import ImageIO
import Vision

private let maxInputBytes = 64 * 1024 * 1024
private let inputChunkBytes = 1024 * 1024

private func emit(_ payload: [String: Any]) {
    guard JSONSerialization.isValidJSONObject(payload),
          let data = try? JSONSerialization.data(withJSONObject: payload) else {
        FileHandle.standardOutput.write(Data("{\"ok\":false}".utf8))
        return
    }
    FileHandle.standardOutput.write(data)
}

private func fail(_ message: String, code: Int32 = 1) -> Never {
    emit(["ok": false])
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(code)
}

private func readBoundedInput() -> Data {
    var input = Data()
    do {
        while input.count <= maxInputBytes {
            let remaining = maxInputBytes + 1 - input.count
            guard remaining > 0 else { break }
            let chunk = try FileHandle.standardInput.read(
                upToCount: min(inputChunkBytes, remaining)
            ) ?? Data()
            guard !chunk.isEmpty else { break }
            input.append(chunk)
        }
    } catch {
        fail("could not read image input")
    }
    return input
}

if CommandLine.arguments.dropFirst().contains("--check") {
    _ = VNRecognizeTextRequest()
    emit(["ok": true, "texts": [], "boxes": [], "scores": []])
    exit(0)
}

// Pipes may legally return a short read before EOF. Accumulate bounded chunks
// so a large image is never silently truncated, while still reading one byte
// past the contract limit to reject oversized input without unbounded memory.
let input = readBoundedInput()
guard !input.isEmpty else {
    fail("empty image input")
}
guard input.count <= maxInputBytes else {
    fail("image input exceeds 64 MiB limit")
}
if CommandLine.arguments.dropFirst().contains("--check-input") {
    emit(["ok": true, "inputBytes": input.count])
    exit(0)
}
guard let source = CGImageSourceCreateWithData(input as CFData, nil),
      let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
    fail("could not decode image")
}

let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.usesLanguageCorrection = true
if #available(macOS 13.0, *) {
    request.automaticallyDetectsLanguage = true
}

do {
    try VNImageRequestHandler(cgImage: image, options: [:]).perform([request])
} catch {
    fail("Vision text recognition failed: \(error)")
}

let width = CGFloat(image.width)
let height = CGFloat(image.height)
let observations = (request.results ?? []).sorted { lhs, rhs in
    let verticalDelta = lhs.boundingBox.midY - rhs.boundingBox.midY
    if abs(verticalDelta) > 0.01 {
        return verticalDelta > 0
    }
    return lhs.boundingBox.minX < rhs.boundingBox.minX
}

var texts: [String] = []
var boxes: [[Int]] = []
var scores: [Float] = []

for observation in observations {
    guard let candidate = observation.topCandidates(1).first else { continue }
    let rect = observation.boundingBox
    let x0 = max(0, min(image.width, Int((rect.minX * width).rounded(.down))))
    let x1 = max(0, min(image.width, Int((rect.maxX * width).rounded(.up))))
    // Vision coordinates start at bottom-left; Persome boxes start at top-left.
    let y0 = max(0, min(image.height, Int(((1 - rect.maxY) * height).rounded(.down))))
    let y1 = max(0, min(image.height, Int(((1 - rect.minY) * height).rounded(.up))))
    texts.append(candidate.string)
    boxes.append([x0, y0, x1, y1])
    scores.append(candidate.confidence)
}

emit(["ok": true, "texts": texts, "boxes": boxes, "scores": scores])
