import Foundation
import Testing
@testable import PersomeAppleHealth

private struct PairedRelay {
    let phoneStore: MemoryRelayCredentialStore
    let macStore: MemoryRelayCredentialStore
    let phoneCredential: RelayCredential
    let macCredential: RelayCredential
}

private func pairRelay() throws -> PairedRelay {
    var responder = try RelayHandshakeResponder(peerID: "owner-mac", pairingCode: "482193")
    var initiator = try RelayHandshakeInitiator(
        offer: responder.offer,
        ownPeerID: "owner-phone"
    )
    let serverHello = try responder.receive(initiator.makeClientHello())
    let clientConfirmation = try initiator.receive(serverHello)
    let result = try responder.finish(clientConfirmation)
    let phoneCredential = try initiator.finish(result.complete)
    let phoneStore = MemoryRelayCredentialStore()
    let macStore = MemoryRelayCredentialStore()
    try phoneStore.save(phoneCredential)
    try macStore.save(result.credential)
    return PairedRelay(
        phoneStore: phoneStore,
        macStore: macStore,
        phoneCredential: phoneCredential,
        macCredential: result.credential
    )
}

@Test func confirmedSessionsExchangeBidirectionally() throws {
    let paired = try pairRelay()
    let phone = DurableSecureRelaySession(peerID: "owner-mac", store: paired.phoneStore)
    let mac = DurableSecureRelaySession(peerID: "owner-phone", store: paired.macStore)

    let upload = try phone.seal(Data("health batch".utf8))
    #expect(try mac.open(upload) == Data("health batch".utf8))

    let receipt = try mac.seal(Data("inserted: 4".utf8))
    #expect(try phone.open(receipt) == Data("inserted: 4".utf8))
}

@Test func rejectsTamperedTranscriptAndKeyConfirmation() throws {
    var responder = try RelayHandshakeResponder(peerID: "owner-mac", pairingCode: "482193")
    var initiator = try RelayHandshakeInitiator(
        offer: responder.offer,
        ownPeerID: "owner-phone"
    )
    let server = try responder.receive(initiator.makeClientHello())
    let tampered = RelayServerHello(
        version: server.version,
        sessionID: server.sessionID,
        transcriptHash: server.transcriptHash,
        keyConfirmation: Data(repeating: 0, count: server.keyConfirmation.count),
        sessionExpiresAt: server.sessionExpiresAt
    )
    #expect(throws: SecureRelayError.keyConfirmationFailed) {
        try initiator.receive(tampered)
    }

    let alteredOffer = try RelayPairingOffer(
        sessionID: responder.offer.sessionID,
        responderPeerID: responder.offer.responderPeerID,
        responderIdentity: responder.offer.responderIdentity,
        responderNonce: responder.offer.responderNonce,
        pairingSecret: Data(repeating: 7, count: 32),
        pairingCode: responder.offer.pairingCode,
        expiresAt: responder.offer.expiresAt
    )
    let attacker = try RelayHandshakeInitiator(offer: alteredOffer, ownPeerID: "attacker")
    #expect(throws: SecureRelayError.keyConfirmationFailed) {
        try responder.receive(attacker.makeClientHello())
    }
}

@Test func durableCountersSurviveRestartAndRejectOldCiphertext() throws {
    let paired = try pairRelay()
    let firstPhoneProcess = DurableSecureRelaySession(
        peerID: "owner-mac",
        store: paired.phoneStore
    )
    let firstMacProcess = DurableSecureRelaySession(
        peerID: "owner-phone",
        store: paired.macStore
    )
    let first = try firstPhoneProcess.seal(Data("first".utf8))
    #expect(first.sequence == 0)
    #expect(try firstMacProcess.open(first) == Data("first".utf8))

    // Simulate a process boundary by round-tripping exactly the Codable value
    // stored by Keychain into entirely new store/session instances.
    let loadedPersistedPhone = try paired.phoneStore.load(peerID: "owner-mac")
    let loadedPersistedMac = try paired.macStore.load(peerID: "owner-phone")
    let persistedPhone = try #require(loadedPersistedPhone)
    let persistedMac = try #require(loadedPersistedMac)
    let restartedPhoneStore = MemoryRelayCredentialStore()
    let restartedMacStore = MemoryRelayCredentialStore()
    try restartedPhoneStore.save(
        JSONDecoder.persome.decode(
            RelayCredential.self,
            from: JSONEncoder.persome.encode(persistedPhone)
        )
    )
    try restartedMacStore.save(
        JSONDecoder.persome.decode(
            RelayCredential.self,
            from: JSONEncoder.persome.encode(persistedMac)
        )
    )
    let restartedPhoneProcess = DurableSecureRelaySession(
        peerID: "owner-mac",
        store: restartedPhoneStore
    )
    let restartedMacProcess = DurableSecureRelaySession(
        peerID: "owner-phone",
        store: restartedMacStore
    )
    let second = try restartedPhoneProcess.seal(Data("second".utf8))
    #expect(second.sequence == 1)
    #expect(try restartedMacProcess.open(second) == Data("second".utf8))
    #expect(throws: SecureRelayError.replayedMessage) {
        try restartedMacProcess.open(first)
    }

    let loadedPhoneCredential = try restartedPhoneStore.load(peerID: "owner-mac")
    let loadedMacCredential = try restartedMacStore.load(peerID: "owner-phone")
    let phoneCredential = try #require(loadedPhoneCredential)
    let macCredential = try #require(loadedMacCredential)
    #expect(phoneCredential.nextSendSequence == 2)
    #expect(macCredential.lastReceivedSequence == 1)
}

@Test func authenticatedHeaderBindsSessionAndSequence() throws {
    let paired = try pairRelay()
    let phone = DurableSecureRelaySession(peerID: "owner-mac", store: paired.phoneStore)
    let mac = DurableSecureRelaySession(peerID: "owner-phone", store: paired.macStore)
    let envelope = try phone.seal(Data("private health data".utf8))

    let wrongSequence = RelayEnvelope(
        sessionID: envelope.sessionID,
        sequence: envelope.sequence + 1,
        sealed: envelope.sealed
    )
    #expect(throws: (any Error).self) { try mac.open(wrongSequence) }

    let wrongSession = RelayEnvelope(
        sessionID: UUID().uuidString,
        sequence: envelope.sequence,
        sealed: envelope.sealed
    )
    #expect(throws: SecureRelayError.sessionMismatch) { try mac.open(wrongSession) }
}

@Test func everyPairingCreatesFreshSessionMaterial() throws {
    let first = try pairRelay()
    let second = try pairRelay()
    #expect(first.phoneCredential.sessionID != second.phoneCredential.sessionID)
    #expect(first.phoneCredential.rootKey != second.phoneCredential.rootKey)

    let firstEnvelope = try DurableSecureRelaySession(
        peerID: "owner-mac",
        store: first.phoneStore
    ).seal(Data("same".utf8))
    let secondEnvelope = try DurableSecureRelaySession(
        peerID: "owner-mac",
        store: second.phoneStore
    ).seal(Data("same".utf8))
    #expect(firstEnvelope.sequence == secondEnvelope.sequence)
    #expect(firstEnvelope.sessionID != secondEnvelope.sessionID)
    #expect(firstEnvelope.sealed != secondEnvelope.sealed)
}

@Test func validatesPairingCodeAndOfferExpiry() throws {
    #expect(throws: SecureRelayError.invalidPairingCode) {
        try RelayHandshakeResponder(peerID: "owner-mac", pairingCode: "12345x")
    }

    let now = Date()
    let responder = try RelayHandshakeResponder(
        peerID: "owner-mac",
        pairingCode: "123456",
        now: now,
        offerLifetime: 1
    )
    #expect(throws: SecureRelayError.pairingExpired) {
        try RelayHandshakeInitiator(
            offer: responder.offer,
            ownPeerID: "phone",
            now: now.addingTimeInterval(2)
        )
    }
}
