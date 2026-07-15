import CryptoKit
import Foundation
import Security

public enum RelayRole: String, Codable, Sendable {
    case initiator
    case responder
}

public struct RelayPublicIdentity: Codable, Sendable, Equatable {
    public let keyAgreementKey: Data

    public init(keyAgreementKey: Data) {
        self.keyAgreementKey = keyAgreementKey
    }
}

public struct RelayIdentity: Sendable {
    private let privateKey: P256.KeyAgreement.PrivateKey

    public var publicIdentity: RelayPublicIdentity {
        RelayPublicIdentity(keyAgreementKey: privateKey.publicKey.x963Representation)
    }

    public init() {
        privateKey = P256.KeyAgreement.PrivateKey()
    }

    public init(rawRepresentation: Data) throws {
        privateKey = try P256.KeyAgreement.PrivateKey(rawRepresentation: rawRepresentation)
    }

    public var rawRepresentation: Data { privateKey.rawRepresentation }

    fileprivate func sharedSecret(with peer: RelayPublicIdentity) throws -> SharedSecret {
        let peerKey = try P256.KeyAgreement.PublicKey(x963Representation: peer.keyAgreementKey)
        return try privateKey.sharedSecretFromKeyAgreement(with: peerKey)
    }
}

/// One short-lived QR payload. The 32-byte secret supplies cryptographic entropy;
/// the six-digit code is only a human key-confirmation display and is never the
/// sole input to session key derivation.
public struct RelayPairingOffer: Codable, Sendable, Equatable {
    public let version: Int
    public let sessionID: String
    public let responderPeerID: String
    public let responderIdentity: RelayPublicIdentity
    public let responderNonce: Data
    public let pairingSecret: Data
    public let pairingCode: String
    public let expiresAt: Date

    public init(
        version: Int = 2,
        sessionID: String,
        responderPeerID: String,
        responderIdentity: RelayPublicIdentity,
        responderNonce: Data,
        pairingSecret: Data,
        pairingCode: String,
        expiresAt: Date
    ) throws {
        guard version == 2 else { throw SecureRelayError.unsupportedVersion }
        guard !sessionID.isEmpty, !responderPeerID.isEmpty else {
            throw SecureRelayError.invalidPairingOffer
        }
        guard responderNonce.count == 32, pairingSecret.count == 32 else {
            throw SecureRelayError.invalidPairingOffer
        }
        guard Self.validPairingCode(pairingCode) else {
            throw SecureRelayError.invalidPairingCode
        }
        self.version = version
        self.sessionID = sessionID
        self.responderPeerID = responderPeerID
        self.responderIdentity = responderIdentity
        self.responderNonce = responderNonce
        self.pairingSecret = pairingSecret
        self.pairingCode = pairingCode
        self.expiresAt = expiresAt
    }

    fileprivate static func validPairingCode(_ code: String) -> Bool {
        code.count == 6 && code.allSatisfy(\.isNumber)
    }

    var isValid: Bool {
        version == 2 && !sessionID.isEmpty && !responderPeerID.isEmpty
            && responderNonce.count == 32 && pairingSecret.count == 32
            && Self.validPairingCode(pairingCode)
    }
}

public struct RelayClientHello: Codable, Sendable, Equatable {
    public let version: Int
    public let sessionID: String
    public let initiatorPeerID: String
    public let responderPeerID: String
    public let initiatorIdentity: RelayPublicIdentity
    public let initiatorNonce: Data
    public let offerProof: Data
}

public struct RelayServerHello: Codable, Sendable, Equatable {
    public let version: Int
    public let sessionID: String
    public let transcriptHash: Data
    public let keyConfirmation: Data
    public let sessionExpiresAt: Date
}

public struct RelayClientConfirmation: Codable, Sendable, Equatable {
    public let version: Int
    public let sessionID: String
    public let keyConfirmation: Data
}

public struct RelayPairingComplete: Codable, Sendable, Equatable {
    public let version: Int
    public let sessionID: String
    public let keyConfirmation: Data
}

public struct RelayHandshakeInitiator: Sendable {
    public let offer: RelayPairingOffer
    public let ownPeerID: String

    private let identity: RelayIdentity
    private let initiatorNonce: Data
    private var rootKey: Data?
    private var transcriptHash: Data?
    private var sessionExpiresAt: Date?

    public init(offer: RelayPairingOffer, ownPeerID: String, now: Date = Date()) throws {
        guard offer.isValid else { throw SecureRelayError.invalidPairingOffer }
        guard offer.expiresAt > now else { throw SecureRelayError.pairingExpired }
        guard !ownPeerID.isEmpty, ownPeerID != offer.responderPeerID else {
            throw SecureRelayError.invalidPairingOffer
        }
        self.offer = offer
        self.ownPeerID = ownPeerID
        identity = RelayIdentity()
        initiatorNonce = try Self.randomBytes(count: 32)
    }

    public func makeClientHello() -> RelayClientHello {
        let unsigned = RelayClientHello(
            version: offer.version,
            sessionID: offer.sessionID,
            initiatorPeerID: ownPeerID,
            responderPeerID: offer.responderPeerID,
            initiatorIdentity: identity.publicIdentity,
            initiatorNonce: initiatorNonce,
            offerProof: Data()
        )
        let transcript = RelayTranscript.bytes(offer: offer, hello: unsigned)
        return RelayClientHello(
            version: unsigned.version,
            sessionID: unsigned.sessionID,
            initiatorPeerID: unsigned.initiatorPeerID,
            responderPeerID: unsigned.responderPeerID,
            initiatorIdentity: unsigned.initiatorIdentity,
            initiatorNonce: unsigned.initiatorNonce,
            offerProof: RelayCrypto.confirmation(
                key: offer.pairingSecret,
                label: "offer-proof",
                transcriptHash: Data(SHA256.hash(data: transcript))
            )
        )
    }

    public mutating func receive(_ hello: RelayServerHello) throws -> RelayClientConfirmation {
        guard hello.version == 2 else { throw SecureRelayError.unsupportedVersion }
        guard hello.sessionID == offer.sessionID else { throw SecureRelayError.sessionMismatch }
        guard hello.sessionExpiresAt > Date() else { throw SecureRelayError.sessionExpired }
        let clientHello = makeClientHello()
        let transcript = RelayTranscript.bytes(offer: offer, hello: clientHello)
        let expectedHash = Data(SHA256.hash(data: transcript))
        guard hello.transcriptHash == expectedHash else {
            throw SecureRelayError.transcriptMismatch
        }
        let derived = try RelayCrypto.deriveRootKey(
            identity: identity,
            peer: offer.responderIdentity,
            pairingSecret: offer.pairingSecret,
            pairingCode: offer.pairingCode,
            sessionID: offer.sessionID,
            transcriptHash: expectedHash
        )
        guard RelayCrypto.verify(
            hello.keyConfirmation,
            key: derived,
            label: "server-confirm",
            transcriptHash: expectedHash
        ) else {
            throw SecureRelayError.keyConfirmationFailed
        }
        rootKey = derived
        transcriptHash = expectedHash
        sessionExpiresAt = hello.sessionExpiresAt
        return RelayClientConfirmation(
            version: 2,
            sessionID: offer.sessionID,
            keyConfirmation: RelayCrypto.confirmation(
                key: derived,
                label: "client-confirm",
                transcriptHash: expectedHash
            )
        )
    }

    public mutating func finish(
        _ complete: RelayPairingComplete,
        now: Date = Date()
    ) throws -> RelayCredential {
        guard complete.version == 2 else { throw SecureRelayError.unsupportedVersion }
        guard complete.sessionID == offer.sessionID else {
            throw SecureRelayError.sessionMismatch
        }
        guard let rootKey, let transcriptHash, let sessionExpiresAt else {
            throw SecureRelayError.handshakeOutOfOrder
        }
        guard RelayCrypto.verify(
            complete.keyConfirmation,
            key: rootKey,
            label: "pairing-complete",
            transcriptHash: transcriptHash
        ) else {
            throw SecureRelayError.keyConfirmationFailed
        }
        return RelayCredential(
            peerID: offer.responderPeerID,
            role: .initiator,
            sessionID: offer.sessionID,
            rootKey: rootKey,
            createdAt: now,
            expiresAt: sessionExpiresAt
        )
    }

    private static func randomBytes(count: Int) throws -> Data {
        var bytes = [UInt8](repeating: 0, count: count)
        let status = SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes)
        guard status == errSecSuccess else { throw SecureRelayError.randomGenerationFailed }
        return Data(bytes)
    }
}

public struct RelayHandshakeResponder: Sendable {
    public let offer: RelayPairingOffer

    private let identity: RelayIdentity
    private var initiatorPeerID: String?
    private var rootKey: Data?
    private var transcriptHash: Data?
    private var sessionExpiresAt: Date?

    public init(
        peerID: String,
        pairingCode: String,
        now: Date = Date(),
        offerLifetime: TimeInterval = 5 * 60
    ) throws {
        guard RelayPairingOffer.validPairingCode(pairingCode) else {
            throw SecureRelayError.invalidPairingCode
        }
        let identity = RelayIdentity()
        self.identity = identity
        offer = try RelayPairingOffer(
            sessionID: UUID().uuidString.lowercased(),
            responderPeerID: peerID,
            responderIdentity: identity.publicIdentity,
            responderNonce: try Self.randomBytes(count: 32),
            pairingSecret: try Self.randomBytes(count: 32),
            pairingCode: pairingCode,
            expiresAt: now.addingTimeInterval(offerLifetime)
        )
    }

    public mutating func receive(
        _ hello: RelayClientHello,
        now: Date = Date(),
        sessionLifetime: TimeInterval = 30 * 24 * 60 * 60
    ) throws -> RelayServerHello {
        guard now < offer.expiresAt else { throw SecureRelayError.pairingExpired }
        guard hello.version == 2 else { throw SecureRelayError.unsupportedVersion }
        guard hello.sessionID == offer.sessionID else { throw SecureRelayError.sessionMismatch }
        guard hello.responderPeerID == offer.responderPeerID,
              !hello.initiatorPeerID.isEmpty,
              hello.initiatorPeerID != offer.responderPeerID,
              hello.initiatorNonce.count == 32
        else {
            throw SecureRelayError.invalidPairingOffer
        }
        let transcript = RelayTranscript.bytes(offer: offer, hello: hello)
        let hash = Data(SHA256.hash(data: transcript))
        guard RelayCrypto.verify(
            hello.offerProof,
            key: offer.pairingSecret,
            label: "offer-proof",
            transcriptHash: hash
        ) else {
            throw SecureRelayError.keyConfirmationFailed
        }
        let derived = try RelayCrypto.deriveRootKey(
            identity: identity,
            peer: hello.initiatorIdentity,
            pairingSecret: offer.pairingSecret,
            pairingCode: offer.pairingCode,
            sessionID: offer.sessionID,
            transcriptHash: hash
        )
        let expiration = now.addingTimeInterval(sessionLifetime)
        initiatorPeerID = hello.initiatorPeerID
        rootKey = derived
        transcriptHash = hash
        sessionExpiresAt = expiration
        return RelayServerHello(
            version: 2,
            sessionID: offer.sessionID,
            transcriptHash: hash,
            keyConfirmation: RelayCrypto.confirmation(
                key: derived,
                label: "server-confirm",
                transcriptHash: hash
            ),
            sessionExpiresAt: expiration
        )
    }

    public mutating func finish(
        _ confirmation: RelayClientConfirmation,
        now: Date = Date()
    ) throws -> (complete: RelayPairingComplete, credential: RelayCredential) {
        guard confirmation.version == 2 else { throw SecureRelayError.unsupportedVersion }
        guard confirmation.sessionID == offer.sessionID else {
            throw SecureRelayError.sessionMismatch
        }
        guard let rootKey, let transcriptHash, let initiatorPeerID, let sessionExpiresAt else {
            throw SecureRelayError.handshakeOutOfOrder
        }
        guard RelayCrypto.verify(
            confirmation.keyConfirmation,
            key: rootKey,
            label: "client-confirm",
            transcriptHash: transcriptHash
        ) else {
            throw SecureRelayError.keyConfirmationFailed
        }
        let credential = RelayCredential(
            peerID: initiatorPeerID,
            role: .responder,
            sessionID: offer.sessionID,
            rootKey: rootKey,
            createdAt: now,
            expiresAt: sessionExpiresAt
        )
        let complete = RelayPairingComplete(
            version: 2,
            sessionID: offer.sessionID,
            keyConfirmation: RelayCrypto.confirmation(
                key: rootKey,
                label: "pairing-complete",
                transcriptHash: transcriptHash
            )
        )
        return (complete, credential)
    }

    private static func randomBytes(count: Int) throws -> Data {
        var bytes = [UInt8](repeating: 0, count: count)
        let status = SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes)
        guard status == errSecSuccess else { throw SecureRelayError.randomGenerationFailed }
        return Data(bytes)
    }
}

public struct RelayEnvelope: Codable, Sendable, Equatable {
    public let version: Int
    public let sessionID: String
    public let sequence: UInt64
    public let sealed: Data

    public init(version: Int = 2, sessionID: String, sequence: UInt64, sealed: Data) {
        self.version = version
        self.sessionID = sessionID
        self.sequence = sequence
        self.sealed = sealed
    }
}

/// A durable encrypted channel. Sequence allocation and receive-watermark
/// advancement are committed to the credential store before ciphertext or
/// plaintext leaves this object, so process restart cannot reuse a nonce or
/// replay an already-authenticated request into the Runtime.
public final class DurableSecureRelaySession: @unchecked Sendable {
    private let peerID: String
    private let store: any RelayCredentialStore

    public init(peerID: String, store: any RelayCredentialStore) {
        self.peerID = peerID
        self.store = store
    }

    public func seal(_ plaintext: Data, now: Date = Date()) throws -> RelayEnvelope {
        let reservation = try store.reserveSendSequence(peerID: peerID, now: now)
        let credential = reservation.credential
        let key = RelayCrypto.directionalKey(
            rootKey: credential.rootKey,
            sessionID: credential.sessionID,
            role: credential.role,
            sending: true
        )
        let nonce = try RelayCrypto.nonce(
            rootKey: credential.rootKey,
            sessionID: credential.sessionID,
            role: credential.role,
            sending: true,
            sequence: reservation.sequence
        )
        let authenticatedData = RelayCrypto.authenticatedData(
            version: credential.version,
            sessionID: credential.sessionID,
            sequence: reservation.sequence
        )
        let box = try AES.GCM.seal(
            plaintext,
            using: key,
            nonce: nonce,
            authenticating: authenticatedData
        )
        guard let combined = box.combined else { throw SecureRelayError.invalidEnvelope }
        return RelayEnvelope(
            version: credential.version,
            sessionID: credential.sessionID,
            sequence: reservation.sequence,
            sealed: combined
        )
    }

    public func open(_ envelope: RelayEnvelope, now: Date = Date()) throws -> Data {
        guard envelope.version == 2 else { throw SecureRelayError.unsupportedVersion }
        guard let credential = try store.load(peerID: peerID) else {
            throw SecureRelayError.credentialNotFound
        }
        guard credential.expiresAt > now else { throw SecureRelayError.sessionExpired }
        guard credential.sessionID == envelope.sessionID else {
            throw SecureRelayError.sessionMismatch
        }
        if let last = credential.lastReceivedSequence, envelope.sequence <= last {
            throw SecureRelayError.replayedMessage
        }
        let key = RelayCrypto.directionalKey(
            rootKey: credential.rootKey,
            sessionID: credential.sessionID,
            role: credential.role,
            sending: false
        )
        let box = try AES.GCM.SealedBox(combined: envelope.sealed)
        let plaintext = try AES.GCM.open(
            box,
            using: key,
            authenticating: RelayCrypto.authenticatedData(
                version: envelope.version,
                sessionID: envelope.sessionID,
                sequence: envelope.sequence
            )
        )
        try store.acceptReceivedSequence(
            peerID: peerID,
            sessionID: envelope.sessionID,
            sequence: envelope.sequence,
            now: now
        )
        return plaintext
    }
}

public enum SecureRelayError: Error, Equatable {
    case credentialNotFound
    case handshakeOutOfOrder
    case invalidEnvelope
    case invalidPairingCode
    case invalidPairingOffer
    case keyConfirmationFailed
    case pairingExpired
    case randomGenerationFailed
    case replayedMessage
    case sequenceExhausted
    case sessionExpired
    case sessionMismatch
    case transcriptMismatch
    case unsupportedVersion
}

private enum RelayTranscript {
    static func bytes(offer: RelayPairingOffer, hello: RelayClientHello) -> Data {
        fields([
            Data("persome-health-relay-handshake-v2".utf8),
            Data(String(offer.version).utf8),
            Data(offer.sessionID.utf8),
            Data(offer.responderPeerID.utf8),
            Data(hello.initiatorPeerID.utf8),
            offer.responderIdentity.keyAgreementKey,
            hello.initiatorIdentity.keyAgreementKey,
            offer.responderNonce,
            hello.initiatorNonce,
            Data(offer.pairingCode.utf8),
        ])
    }

    private static func fields(_ values: [Data]) -> Data {
        var result = Data()
        for value in values {
            var length = UInt64(value.count).bigEndian
            withUnsafeBytes(of: &length) { result.append(contentsOf: $0) }
            result.append(value)
        }
        return result
    }
}

private enum RelayCrypto {
    static func deriveRootKey(
        identity: RelayIdentity,
        peer: RelayPublicIdentity,
        pairingSecret: Data,
        pairingCode: String,
        sessionID: String,
        transcriptHash: Data
    ) throws -> Data {
        let secret = try identity.sharedSecret(with: peer)
        let key = secret.hkdfDerivedSymmetricKey(
            using: SHA256.self,
            salt: pairingSecret,
            sharedInfo: RelayTranscript.fieldsForKey(
                sessionID: sessionID,
                pairingCode: pairingCode,
                transcriptHash: transcriptHash
            ),
            outputByteCount: 32
        )
        return key.withUnsafeBytes { Data($0) }
    }

    static func confirmation(
        key: Data,
        label: String,
        transcriptHash: Data
    ) -> Data {
        let input = Data("persome-relay-v2:\(label):".utf8) + transcriptHash
        return Data(HMAC<SHA256>.authenticationCode(for: input, using: SymmetricKey(data: key)))
    }

    static func verify(
        _ received: Data,
        key: Data,
        label: String,
        transcriptHash: Data
    ) -> Bool {
        let expected = confirmation(key: key, label: label, transcriptHash: transcriptHash)
        return HMAC<SHA256>.isValidAuthenticationCode(
            received,
            authenticating: Data("persome-relay-v2:\(label):".utf8) + transcriptHash,
            using: SymmetricKey(data: key)
        ) && received == expected
    }

    static func directionalKey(
        rootKey: Data,
        sessionID: String,
        role: RelayRole,
        sending: Bool
    ) -> SymmetricKey {
        let label = directionLabel(role: role, sending: sending)
        return HKDF<SHA256>.deriveKey(
            inputKeyMaterial: SymmetricKey(data: rootKey),
            salt: Data("persome-relay-direction-v2".utf8),
            info: Data("\(sessionID):\(label):key".utf8),
            outputByteCount: 32
        )
    }

    static func nonce(
        rootKey: Data,
        sessionID: String,
        role: RelayRole,
        sending: Bool,
        sequence: UInt64
    ) throws -> AES.GCM.Nonce {
        let label = directionLabel(role: role, sending: sending)
        let prefixKey = HKDF<SHA256>.deriveKey(
            inputKeyMaterial: SymmetricKey(data: rootKey),
            salt: Data("persome-relay-nonce-v2".utf8),
            info: Data("\(sessionID):\(label):nonce".utf8),
            outputByteCount: 4
        )
        var data = prefixKey.withUnsafeBytes { Data($0) }
        var bigEndian = sequence.bigEndian
        withUnsafeBytes(of: &bigEndian) { data.append(contentsOf: $0) }
        return try AES.GCM.Nonce(data: data)
    }

    static func authenticatedData(version: Int, sessionID: String, sequence: UInt64) -> Data {
        Data("persome-relay-envelope:\(version):\(sessionID):\(sequence)".utf8)
    }

    private static func directionLabel(role: RelayRole, sending: Bool) -> String {
        switch (role, sending) {
        case (.initiator, true), (.responder, false): "initiator-to-responder"
        case (.responder, true), (.initiator, false): "responder-to-initiator"
        }
    }
}

private extension RelayTranscript {
    static func fieldsForKey(sessionID: String, pairingCode: String, transcriptHash: Data) -> Data {
        var result = Data("persome-relay-root-v2".utf8)
        result.append(Data(sessionID.utf8))
        result.append(Data(pairingCode.utf8))
        result.append(transcriptHash)
        return result
    }
}
