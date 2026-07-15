import Foundation
import Testing
@testable import PersomeAppleHealth

private actor MockHealthRuntimeForwarder: HealthRuntimeForwarding {
    private var bodies: [HealthEventsImport] = []

    func importChanges(_ body: HealthEventsImport) async throws -> HealthImportResult {
        bodies.append(body)
        return HealthImportResult(
            schemaVersion: 1,
            received: body.events.count,
            inserted: body.events.count,
            corrected: 2,
            duplicates: 0,
            deleted: body.deletedEvents.count
        )
    }

    func snapshot() -> [HealthEventsImport] { bodies }
}

private actor InMemoryRelayChannel: RelayRequestChannel {
    private var host: MacHealthRelayHost
    private let phonePeerID: String
    private var recordedEnvelope: RelayEnvelope?

    init(host: MacHealthRelayHost, phonePeerID: String) {
        self.host = host
        self.phonePeerID = phonePeerID
    }

    func request(_ frame: RelayWireFrame, to _: String) async throws -> RelayWireFrame {
        if case let .envelope(envelope) = frame { recordedEnvelope = envelope }
        return await host.handle(frame, from: phonePeerID)
    }

    func replaceHost(_ newHost: MacHealthRelayHost) {
        host = newHost
    }

    func lastEnvelope() -> RelayEnvelope? { recordedEnvelope }
}

private func sampleEvent(metadata: [String: String] = [:]) -> HealthEvent {
    HealthEvent(
        eventID: "sample-1",
        source: HealthEventSource(device: "Apple Watch", deviceID: "local-watch"),
        metric: "heart_rate",
        value: .number(72),
        unit: "bpm",
        startedAt: Date(timeIntervalSince1970: 1_700_000_000),
        endedAt: nil,
        timezone: "Asia/Shanghai",
        metadata: metadata
    )
}

@Test func phoneToMultipeerBoundaryToMacLoopbackPathIsEndToEndAndRestartSafe() async throws {
    let phonePeerID = "owner-phone"
    let macPeerID = "owner-mac"
    let phoneStore = MemoryRelayCredentialStore()
    let macStore = MemoryRelayCredentialStore()
    let forwarder = MockHealthRuntimeForwarder()
    let firstHost = MacHealthRelayHost(
        peerID: macPeerID,
        credentialStore: macStore,
        forwarder: forwarder
    )
    let channel = InMemoryRelayChannel(host: firstHost, phonePeerID: phonePeerID)
    let hostOffer = try await firstHost.beginPairing(pairingCode: "482193")
    let offer = try RelayPairingOfferCodec.decode(RelayPairingOfferCodec.encode(hostOffer))

    let phoneCredential = try await SecureHealthRelayPairer.pair(
        offer: offer,
        confirmedPairingCode: "482193",
        ownPeerID: phonePeerID,
        credentialStore: phoneStore,
        channel: channel
    )
    let loadedMacCredential = try macStore.load(peerID: phonePeerID)
    let macCredential = try #require(loadedMacCredential)
    #expect(phoneCredential.rootKey == macCredential.rootKey)
    #expect(phoneCredential.role == .initiator)
    #expect(macCredential.role == .responder)

    let firstClient = SecureHealthRelayClient(
        macPeerID: macPeerID,
        credentialStore: phoneStore,
        channel: channel
    )
    let firstResult = try await firstClient.upload(
        events: [sampleEvent()],
        deletedEvents: [HealthEventDeletion(eventID: "deleted-1")]
    )
    #expect(firstResult.corrected == 2)
    #expect(firstResult.deleted == 1)
    #expect(await forwarder.snapshot().count == 1)

    // MacHealthRelayHost creates a fresh secure-session object for each frame;
    // replay state comes from the durable store, not process memory.
    let replayed = try #require(await channel.lastEnvelope())
    let replayResponse = await firstHost.handle(.envelope(replayed), from: phonePeerID)
    guard case let .failure(failure) = replayResponse else {
        Issue.record("replayed ciphertext should be rejected")
        return
    }
    #expect(failure.code == "secure_session_rejected")
    #expect(await forwarder.snapshot().count == 1)

    // Replace the host and client objects while retaining only their credential
    // stores. The next request uses the next durable sequence and still succeeds.
    let restartedHost = MacHealthRelayHost(
        peerID: macPeerID,
        credentialStore: macStore,
        forwarder: forwarder
    )
    await channel.replaceHost(restartedHost)
    let restartedClient = SecureHealthRelayClient(
        macPeerID: macPeerID,
        credentialStore: phoneStore,
        channel: channel
    )
    let secondResult = try await restartedClient.upload(
        events: [],
        deletedEvents: [HealthEventDeletion(eventID: "deleted-2")]
    )
    #expect(secondResult.deleted == 1)
    #expect(await forwarder.snapshot().count == 2)
}

@Test func bearerClientAndMacForwarderRejectNonLoopbackRuntime() {
    let lanURL = URL(string: "http://192.168.1.10:8742")!
    #expect(throws: PersomeHealthClientError.nonLoopbackRuntime) {
        try PersomeHealthClient(runtimeURL: lanURL, bearerToken: "owner-token")
    }
    #expect(throws: PersomeHealthClientError.nonLoopbackRuntime) {
        try LoopbackHealthRuntimeForwarder(
            runtimeURL: lanURL,
            bearerToken: "owner-token"
        )
    }
}

@Test func pairingPayloadAndRelayPlaintextAreBounded() async throws {
    let forwarder = MockHealthRuntimeForwarder()
    let host = MacHealthRelayHost(
        peerID: "owner-mac",
        credentialStore: MemoryRelayCredentialStore(),
        forwarder: forwarder
    )
    let offer = try await host.beginPairing(pairingCode: "482193")
    let encodedOffer = try RelayPairingOfferCodec.encode(offer)
    let decodedOffer = try RelayPairingOfferCodec.decode(encodedOffer)
    #expect(decodedOffer.sessionID == offer.sessionID)
    #expect(decodedOffer.pairingSecret == offer.pairingSecret)
    var malformedObject = try #require(
        JSONSerialization.jsonObject(with: encodedOffer) as? [String: Any]
    )
    malformedObject["pairingSecret"] = Data([1]).base64EncodedString()
    let malformedOffer = try JSONSerialization.data(withJSONObject: malformedObject)
    #expect(throws: HealthRelayError.invalidPairingPayload) {
        try RelayPairingOfferCodec.decode(malformedOffer)
    }

    let phoneStore = MemoryRelayCredentialStore()
    let channel = InMemoryRelayChannel(host: host, phonePeerID: "owner-phone")
    _ = try await SecureHealthRelayPairer.pair(
        offer: offer,
        confirmedPairingCode: "482193",
        ownPeerID: "owner-phone",
        credentialStore: phoneStore,
        channel: channel
    )
    await #expect(throws: HealthRelayError.pairingCodeMismatch) {
        try await SecureHealthRelayPairer.pair(
            offer: offer,
            confirmedPairingCode: "000000",
            ownPeerID: "other-phone",
            credentialStore: MemoryRelayCredentialStore(),
            channel: channel
        )
    }
    let client = SecureHealthRelayClient(
        macPeerID: "owner-mac",
        credentialStore: phoneStore,
        channel: channel
    )
    await #expect(throws: HealthRelayError.payloadTooLarge) {
        try await client.upload(
            events: [sampleEvent(metadata: ["blob": String(repeating: "x", count: 2_100_000)])],
            deletedEvents: []
        )
    }
    #expect(await forwarder.snapshot().isEmpty)
}
