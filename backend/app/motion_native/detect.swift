import Foundation
import Vision

struct Box: Encodable {
    let x: Double
    let y: Double
    let width: Double
    let height: Double
    let confidence: Float
}
struct Frame: Encodable {
    let source_ms: Int
    let faces: [Box]
}
guard CommandLine.arguments.count == 3,
      let frequency = Int(CommandLine.arguments[2]), (1...3).contains(frequency) else {
    fatalError("Expected a frame directory and sample frequency.")
}
let directory = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
let images = try FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil)
    .filter { $0.pathExtension == "jpg" }.sorted { $0.lastPathComponent < $1.lastPathComponent }
let request = VNDetectFaceRectanglesRequest()
request.revision = VNDetectFaceRectanglesRequestRevision3
request.usesCPUOnly = true
for (index, image) in images.enumerated() {
    try VNImageRequestHandler(url: image, orientation: .up).perform([request])
    let boxes = (request.results ?? []).prefix(30).compactMap { observation -> Box? in
        let r = observation.boundingBox.intersection(CGRect(x: 0, y: 0, width: 1, height: 1))
        guard !r.isNull && r.width > 0 && r.height > 0 else { return nil }
        return Box(x: r.minX, y: 1-r.maxY, width: r.width, height: r.height, confidence: observation.confidence)
    }
    let frame = Frame(source_ms: index * 1000 / frequency, faces: boxes)
    FileHandle.standardOutput.write(try JSONEncoder().encode(frame))
    FileHandle.standardOutput.write(Data([10]))
}
