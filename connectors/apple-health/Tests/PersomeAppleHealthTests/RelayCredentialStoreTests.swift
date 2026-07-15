import Foundation
import Testing
@testable import PersomeAppleHealth

private func credential(
    sessionID: String = "session-1",
    rootByte: UInt8 = 1
) -> RelayCredential {
    RelayCredential(
        peerID: "owner-mac",
        role: .initiator,
        sessionID: sessionID,
        rootKey: Data(repeating: rootByte, count: 32),
        expiresAt: Date().addingTimeInterval(3_600)
    )
}

@Test func credentialStoreRoundTripPersistsSessionState() throws {
    let store = MemoryRelayCredentialStore()
    let value = credential()
    try store.save(value)

    #expect(try store.load(peerID: "owner-mac") == value)
    let reservation = try store.reserveSendSequence(peerID: "owner-mac", now: Date())
    #expect(reservation.sequence == 0)
    #expect(try store.load(peerID: "owner-mac")?.nextSendSequence == 1)

    try store.acceptReceivedSequence(
        peerID: "owner-mac",
        sessionID: value.sessionID,
        sequence: 8,
        now: Date()
    )
    #expect(try store.load(peerID: "owner-mac")?.lastReceivedSequence == 8)
    #expect(throws: SecureRelayError.replayedMessage) {
        try store.acceptReceivedSequence(
            peerID: "owner-mac",
            sessionID: value.sessionID,
            sequence: 8,
            now: Date()
        )
    }

    try store.delete(peerID: "owner-mac")
    #expect(try store.load(peerID: "owner-mac") == nil)
}

@Test func savingSamePeerReplacesTheWholeSession() throws {
    let store = MemoryRelayCredentialStore()
    let first = credential()
    let replacement = credential(sessionID: "session-2", rootByte: 2)

    try store.save(first)
    try store.save(replacement)
    #expect(try store.load(peerID: "owner-mac") == replacement)
}

@Test func rejectsExpiredAndMalformedCredentials() throws {
    let store = MemoryRelayCredentialStore()
    let expired = RelayCredential(
        peerID: "owner-mac",
        role: .initiator,
        sessionID: "expired",
        rootKey: Data(repeating: 1, count: 32),
        createdAt: Date().addingTimeInterval(-10),
        expiresAt: Date().addingTimeInterval(-1)
    )
    try store.save(expired)
    #expect(throws: SecureRelayError.sessionExpired) {
        try store.reserveSendSequence(peerID: "owner-mac", now: Date())
    }

    let malformed = RelayCredential(
        peerID: "owner-mac",
        role: .initiator,
        sessionID: "bad-key",
        rootKey: Data([1, 2, 3]),
        expiresAt: Date().addingTimeInterval(60)
    )
    #expect(throws: KeychainRelayError.invalidCredential) { try store.save(malformed) }
}
