import Foundation

public enum HealthRelayError: Error, Equatable {
    case invalidPairingPayload
    case pairingCodeMismatch
    case pairingRejected(String)
    case payloadTooLarge
    case requestMismatch
    case runtimeRejected(String)
    case unexpectedFrame
}

public enum RelayPairingOfferCodec {
    public static let maximumPayloadBytes = 4 * 1024

    public static func encode(_ offer: RelayPairingOffer) throws -> Data {
        let data = try JSONEncoder.persome.encode(offer)
        guard data.count <= maximumPayloadBytes else { throw HealthRelayError.payloadTooLarge }
        return data
    }

    public static func decode(_ data: Data, now: Date = Date()) throws -> RelayPairingOffer {
        guard data.count <= maximumPayloadBytes else { throw HealthRelayError.payloadTooLarge }
        do {
            let offer = try JSONDecoder.persome.decode(RelayPairingOffer.self, from: data)
            guard offer.isValid, offer.expiresAt > now else {
                throw HealthRelayError.invalidPairingPayload
            }
            return offer
        } catch let error as HealthRelayError {
            throw error
        } catch {
            throw HealthRelayError.invalidPairingPayload
        }
    }
}

public struct HealthRelayImportRequest: Codable, Sendable, Equatable {
    public let version: Int
    public let requestID: String
    public let body: HealthEventsImport

    public init(version: Int = 1, requestID: String, body: HealthEventsImport) {
        self.version = version
        self.requestID = requestID
        self.body = body
    }
}

public struct HealthRelayImportResponse: Codable, Sendable, Equatable {
    public let version: Int
    public let requestID: String
    public let result: HealthImportResult?
    public let errorCode: String?

    enum CodingKeys: String, CodingKey {
        case version, result
        case requestID = "request_id"
        case errorCode = "error_code"
    }

    public init(
        version: Int = 1,
        requestID: String,
        result: HealthImportResult? = nil,
        errorCode: String? = nil
    ) {
        self.version = version
        self.requestID = requestID
        self.result = result
        self.errorCode = errorCode
    }
}

extension HealthEventsImport: Equatable {
    public static func == (lhs: HealthEventsImport, rhs: HealthEventsImport) -> Bool {
        lhs.events == rhs.events && lhs.deletedEvents == rhs.deletedEvents
    }
}

public protocol HealthRuntimeForwarding: Sendable {
    func importChanges(_ body: HealthEventsImport) async throws -> HealthImportResult
}

/// The only component that owns the Runtime bearer. Initialization rejects LAN
/// URLs, so relay plaintext can only be forwarded into the Mac's loopback API.
public actor LoopbackHealthRuntimeForwarder: HealthRuntimeForwarding {
    private let client: PersomeHealthClient

    public init(
        runtimeURL: URL = URL(string: "http://127.0.0.1:8742")!,
        bearerToken: String,
        session: URLSession = .shared
    ) throws {
        guard !bearerToken.isEmpty else { throw HealthRelayError.runtimeRejected("missing_token") }
        client = try PersomeHealthClient(
            runtimeURL: runtimeURL,
            bearerToken: bearerToken,
            session: session
        )
    }

    public func importChanges(_ body: HealthEventsImport) async throws -> HealthImportResult {
        try await client.upload(events: body.events, deletedEvents: body.deletedEvents)
    }
}

/// iPhone-side uploader. It contains only a paired session credential and sends
/// an encrypted request through Multipeer; it has no Runtime URL or bearer.
public actor SecureHealthRelayClient: HealthEventUploader {
    public static let maximumPlaintextBytes = 2 * 1024 * 1024

    private let macPeerID: String
    private let channel: any RelayRequestChannel
    private let session: DurableSecureRelaySession

    public init(
        macPeerID: String,
        credentialStore: any RelayCredentialStore,
        channel: any RelayRequestChannel
    ) {
        self.macPeerID = macPeerID
        self.channel = channel
        session = DurableSecureRelaySession(peerID: macPeerID, store: credentialStore)
    }

    public func upload(
        events: [HealthEvent],
        deletedEvents: [HealthEventDeletion]
    ) async throws -> HealthImportResult {
        guard !events.isEmpty || !deletedEvents.isEmpty,
              events.count + deletedEvents.count <= 1_000
        else {
            throw HealthRelayError.payloadTooLarge
        }
        let requestID = UUID().uuidString.lowercased()
        let request = HealthRelayImportRequest(
            requestID: requestID,
            body: HealthEventsImport(events: events, deletedEvents: deletedEvents)
        )
        let plaintext = try JSONEncoder.persome.encode(request)
        guard plaintext.count <= Self.maximumPlaintextBytes else {
            throw HealthRelayError.payloadTooLarge
        }
        let outgoing = try session.seal(plaintext)
        let responseFrame = try await channel.request(.envelope(outgoing), to: macPeerID)
        switch responseFrame {
        case let .envelope(incoming):
            let responseData = try session.open(incoming)
            let response = try JSONDecoder.persome.decode(
                HealthRelayImportResponse.self,
                from: responseData
            )
            guard response.version == 1, response.requestID == requestID else {
                throw HealthRelayError.requestMismatch
            }
            if let code = response.errorCode { throw HealthRelayError.runtimeRejected(code) }
            guard let result = response.result else { throw HealthRelayError.unexpectedFrame }
            return result
        case let .failure(failure):
            throw HealthRelayError.runtimeRejected(failure.code)
        default:
            throw HealthRelayError.unexpectedFrame
        }
    }
}

public enum SecureHealthRelayPairer {
    /// Runs both transcript-confirmation flights over the same request channel
    /// later used for health uploads, then durably stores the confirmed session.
    public static func pair(
        offer: RelayPairingOffer,
        confirmedPairingCode: String,
        ownPeerID: String,
        credentialStore: any RelayCredentialStore,
        channel: any RelayRequestChannel
    ) async throws -> RelayCredential {
        guard confirmedPairingCode == offer.pairingCode else {
            throw HealthRelayError.pairingCodeMismatch
        }
        var handshake = try RelayHandshakeInitiator(offer: offer, ownPeerID: ownPeerID)
        let first = try await channel.request(
            .clientHello(handshake.makeClientHello()),
            to: offer.responderPeerID
        )
        let serverHello: RelayServerHello
        switch first {
        case let .serverHello(value): serverHello = value
        case let .failure(failure): throw HealthRelayError.pairingRejected(failure.code)
        default: throw HealthRelayError.unexpectedFrame
        }

        let confirmation = try handshake.receive(serverHello)
        let second = try await channel.request(
            .clientConfirmation(confirmation),
            to: offer.responderPeerID
        )
        let complete: RelayPairingComplete
        switch second {
        case let .pairingComplete(value): complete = value
        case let .failure(failure): throw HealthRelayError.pairingRejected(failure.code)
        default: throw HealthRelayError.unexpectedFrame
        }
        let credential = try handshake.finish(complete)
        try credentialStore.save(credential)
        return credential
    }
}

/// Mac-side protocol terminus. It owns pairing responder state, durable replay
/// state, and a loopback forwarder. The Runtime bearer never enters a wire frame.
public actor MacHealthRelayHost {
    private let peerID: String
    private let credentialStore: any RelayCredentialStore
    private let forwarder: any HealthRuntimeForwarding
    private var handshakes: [String: RelayHandshakeResponder] = [:]

    public init(
        peerID: String,
        credentialStore: any RelayCredentialStore,
        forwarder: any HealthRuntimeForwarding
    ) {
        self.peerID = peerID
        self.credentialStore = credentialStore
        self.forwarder = forwarder
    }

    public func beginPairing(pairingCode: String) throws -> RelayPairingOffer {
        let handshake = try RelayHandshakeResponder(peerID: peerID, pairingCode: pairingCode)
        handshakes[handshake.offer.sessionID] = handshake
        return handshake.offer
    }

    public func handle(_ frame: RelayWireFrame, from connectedPeerID: String) async -> RelayWireFrame {
        do {
            switch frame {
            case let .clientHello(hello):
                guard hello.initiatorPeerID == connectedPeerID,
                      var handshake = handshakes[hello.sessionID]
                else {
                    throw SecureRelayError.invalidPairingOffer
                }
                let response = try handshake.receive(hello)
                handshakes[hello.sessionID] = handshake
                return .serverHello(response)

            case let .clientConfirmation(confirmation):
                guard var handshake = handshakes[confirmation.sessionID] else {
                    throw SecureRelayError.handshakeOutOfOrder
                }
                let result = try handshake.finish(confirmation)
                guard result.credential.peerID == connectedPeerID else {
                    throw SecureRelayError.invalidPairingOffer
                }
                try credentialStore.save(result.credential)
                handshakes.removeValue(forKey: confirmation.sessionID)
                return .pairingComplete(result.complete)

            case let .envelope(envelope):
                return await handleEnvelope(envelope, from: connectedPeerID)

            default:
                throw HealthRelayError.unexpectedFrame
            }
        } catch {
            return .failure(RelayProtocolFailure(code: "protocol_rejected"))
        }
    }

    private func handleEnvelope(
        _ envelope: RelayEnvelope,
        from connectedPeerID: String
    ) async -> RelayWireFrame {
        do {
            let secureSession = DurableSecureRelaySession(
                peerID: connectedPeerID,
                store: credentialStore
            )
            let plaintext = try secureSession.open(envelope)
            guard plaintext.count <= SecureHealthRelayClient.maximumPlaintextBytes else {
                throw HealthRelayError.payloadTooLarge
            }
            let request = try JSONDecoder.persome.decode(
                HealthRelayImportRequest.self,
                from: plaintext
            )
            guard request.version == 1,
                  !request.body.events.isEmpty || !request.body.deletedEvents.isEmpty,
                  request.body.events.count + request.body.deletedEvents.count <= 1_000
            else {
                throw HealthRelayError.payloadTooLarge
            }
            let response: HealthRelayImportResponse
            do {
                let result = try await forwarder.importChanges(request.body)
                response = HealthRelayImportResponse(requestID: request.requestID, result: result)
            } catch {
                response = HealthRelayImportResponse(
                    requestID: request.requestID,
                    errorCode: "runtime_rejected"
                )
            }
            let responseData = try JSONEncoder.persome.encode(response)
            return .envelope(try secureSession.seal(responseData))
        } catch {
            return .failure(RelayProtocolFailure(code: "secure_session_rejected"))
        }
    }
}

/// Binds the async Mac host to incoming Multipeer frames. The transport sends
/// every returned frame to the exact connected peer that originated it.
public final class MultipeerHealthRelayHostAdapter: @unchecked Sendable {
    private let transport: MultipeerRelayTransport
    private let host: MacHealthRelayHost

    public init(transport: MultipeerRelayTransport, host: MacHealthRelayHost) {
        self.transport = transport
        self.host = host
        transport.onFrame = { [weak transport, host] peerID, frame in
            Task {
                let response = await host.handle(frame, from: peerID)
                do {
                    try transport?.send(response, to: peerID)
                } catch {
                    transport?.onError?(error)
                }
            }
        }
    }
}
