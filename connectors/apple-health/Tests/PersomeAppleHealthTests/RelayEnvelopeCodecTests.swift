import Foundation
import Testing
@testable import PersomeAppleHealth

@Test func relayFrameCodecRoundTrip() throws {
    let envelope = RelayEnvelope(
        sessionID: "session-1",
        sequence: 42,
        sealed: Data([1, 2, 3, 4])
    )
    let frame = RelayWireFrame.envelope(envelope)
    #expect(try RelayFrameCodec.decode(RelayFrameCodec.encode(frame)) == frame)
}

@Test func relayFrameCodecRejectsMalformedAndOversizedFrames() {
    #expect(throws: RelayTransportError.invalidFrame) {
        try RelayFrameCodec.decode(Data("not-json".utf8))
    }
    #expect(throws: RelayTransportError.frameTooLarge) {
        try RelayFrameCodec.decode(Data(repeating: 0, count: RelayFrameCodec.maximumFrameBytes + 1))
    }
}
