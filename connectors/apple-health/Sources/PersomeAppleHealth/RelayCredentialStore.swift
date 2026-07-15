import Foundation
import Security

public struct RelayCredential: Codable, Sendable, Equatable {
    public let version: Int
    public let peerID: String
    public let role: RelayRole
    public let sessionID: String
    public let rootKey: Data
    public let createdAt: Date
    public let expiresAt: Date
    public let nextSendSequence: UInt64
    public let lastReceivedSequence: UInt64?

    public init(
        version: Int = 2,
        peerID: String,
        role: RelayRole,
        sessionID: String,
        rootKey: Data,
        createdAt: Date = Date(),
        expiresAt: Date,
        nextSendSequence: UInt64 = 0,
        lastReceivedSequence: UInt64? = nil
    ) {
        self.version = version
        self.peerID = peerID
        self.role = role
        self.sessionID = sessionID
        self.rootKey = rootKey
        self.createdAt = createdAt
        self.expiresAt = expiresAt
        self.nextSendSequence = nextSendSequence
        self.lastReceivedSequence = lastReceivedSequence
    }

    fileprivate var isValid: Bool {
        version == 2 && !peerID.isEmpty && !sessionID.isEmpty && rootKey.count == 32
            && expiresAt > createdAt
    }

    fileprivate func updating(
        nextSendSequence: UInt64? = nil,
        lastReceivedSequence: UInt64?? = nil
    ) -> Self {
        Self(
            version: version,
            peerID: peerID,
            role: role,
            sessionID: sessionID,
            rootKey: rootKey,
            createdAt: createdAt,
            expiresAt: expiresAt,
            nextSendSequence: nextSendSequence ?? self.nextSendSequence,
            lastReceivedSequence: lastReceivedSequence ?? self.lastReceivedSequence
        )
    }
}

public struct RelaySequenceReservation: Sendable, Equatable {
    public let credential: RelayCredential
    public let sequence: UInt64
}

public protocol RelayCredentialStore: AnyObject, Sendable {
    func save(_ credential: RelayCredential) throws
    func load(peerID: String) throws -> RelayCredential?
    func delete(peerID: String) throws

    /// Atomically persists the increment before returning a sequence. A crash
    /// can create a harmless gap, never key+nonce reuse.
    func reserveSendSequence(peerID: String, now: Date) throws -> RelaySequenceReservation

    /// Atomically advances the replay watermark after authentication and before
    /// a caller can act on the plaintext.
    func acceptReceivedSequence(
        peerID: String,
        sessionID: String,
        sequence: UInt64,
        now: Date
    ) throws
}

public final class KeychainRelayCredentialStore: RelayCredentialStore, @unchecked Sendable {
    private static let lock = NSLock()
    private let service: String

    public init(service: String = "ai.persome.apple-health.relay.v2") {
        self.service = service
    }

    public func save(_ credential: RelayCredential) throws {
        try Self.lock.withLock { try saveUnlocked(credential) }
    }

    public func load(peerID: String) throws -> RelayCredential? {
        try Self.lock.withLock { try loadUnlocked(peerID: peerID) }
    }

    public func delete(peerID: String) throws {
        try Self.lock.withLock {
            let status = SecItemDelete(baseQuery(peerID: peerID) as CFDictionary)
            guard status == errSecSuccess || status == errSecItemNotFound else {
                throw KeychainRelayError.operationFailed(status)
            }
        }
    }

    public func reserveSendSequence(
        peerID: String,
        now: Date
    ) throws -> RelaySequenceReservation {
        try Self.lock.withLock {
            guard let credential = try loadUnlocked(peerID: peerID) else {
                throw SecureRelayError.credentialNotFound
            }
            guard credential.expiresAt > now else { throw SecureRelayError.sessionExpired }
            guard credential.nextSendSequence < UInt64.max else {
                throw SecureRelayError.sequenceExhausted
            }
            let sequence = credential.nextSendSequence
            let updated = credential.updating(nextSendSequence: sequence + 1)
            try saveUnlocked(updated)
            return RelaySequenceReservation(credential: updated, sequence: sequence)
        }
    }

    public func acceptReceivedSequence(
        peerID: String,
        sessionID: String,
        sequence: UInt64,
        now: Date
    ) throws {
        try Self.lock.withLock {
            guard let credential = try loadUnlocked(peerID: peerID) else {
                throw SecureRelayError.credentialNotFound
            }
            guard credential.expiresAt > now else { throw SecureRelayError.sessionExpired }
            guard credential.sessionID == sessionID else {
                throw SecureRelayError.sessionMismatch
            }
            if let last = credential.lastReceivedSequence, sequence <= last {
                throw SecureRelayError.replayedMessage
            }
            try saveUnlocked(credential.updating(lastReceivedSequence: .some(sequence)))
        }
    }

    private func saveUnlocked(_ credential: RelayCredential) throws {
        guard credential.isValid else { throw KeychainRelayError.invalidCredential }
        let data = try JSONEncoder.persome.encode(credential)
        let selector = baseQuery(peerID: credential.peerID)
        let updateStatus = SecItemUpdate(
            selector as CFDictionary,
            [kSecValueData: data] as CFDictionary
        )
        if updateStatus == errSecSuccess { return }
        guard updateStatus == errSecItemNotFound else {
            throw KeychainRelayError.operationFailed(updateStatus)
        }

        var insert = selector
        insert[kSecValueData] = data
        insert[kSecAttrAccessible] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        insert[kSecAttrSynchronizable] = kCFBooleanFalse
        let addStatus = SecItemAdd(insert as CFDictionary, nil)
        guard addStatus == errSecSuccess else {
            throw KeychainRelayError.operationFailed(addStatus)
        }
    }

    private func loadUnlocked(peerID: String) throws -> RelayCredential? {
        var query = baseQuery(peerID: peerID)
        query[kSecReturnData] = kCFBooleanTrue
        query[kSecMatchLimit] = kSecMatchLimitOne
        var result: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess, let data = result as? Data else {
            throw KeychainRelayError.operationFailed(status)
        }
        do {
            let credential = try JSONDecoder.persome.decode(RelayCredential.self, from: data)
            guard credential.isValid else { throw KeychainRelayError.invalidCredential }
            return credential
        } catch let error as KeychainRelayError {
            throw error
        } catch {
            throw KeychainRelayError.invalidCredential
        }
    }

    private func baseQuery(peerID: String) -> [CFString: Any] {
        [
            kSecClass: kSecClassGenericPassword,
            kSecAttrService: service,
            kSecAttrAccount: peerID,
        ]
    }
}

public final class MemoryRelayCredentialStore: RelayCredentialStore, @unchecked Sendable {
    private let lock = NSLock()
    private var credentials: [String: RelayCredential] = [:]

    public init() {}

    public func save(_ credential: RelayCredential) throws {
        guard credential.isValid else { throw KeychainRelayError.invalidCredential }
        lock.withLock { credentials[credential.peerID] = credential }
    }

    public func load(peerID: String) throws -> RelayCredential? {
        lock.withLock { credentials[peerID] }
    }

    public func delete(peerID: String) throws {
        _ = lock.withLock { credentials.removeValue(forKey: peerID) }
    }

    public func reserveSendSequence(
        peerID: String,
        now: Date
    ) throws -> RelaySequenceReservation {
        try lock.withLock {
            guard let credential = credentials[peerID] else {
                throw SecureRelayError.credentialNotFound
            }
            guard credential.expiresAt > now else { throw SecureRelayError.sessionExpired }
            guard credential.nextSendSequence < UInt64.max else {
                throw SecureRelayError.sequenceExhausted
            }
            let sequence = credential.nextSendSequence
            let updated = credential.updating(nextSendSequence: sequence + 1)
            credentials[peerID] = updated
            return RelaySequenceReservation(credential: updated, sequence: sequence)
        }
    }

    public func acceptReceivedSequence(
        peerID: String,
        sessionID: String,
        sequence: UInt64,
        now: Date
    ) throws {
        try lock.withLock {
            guard let credential = credentials[peerID] else {
                throw SecureRelayError.credentialNotFound
            }
            guard credential.expiresAt > now else { throw SecureRelayError.sessionExpired }
            guard credential.sessionID == sessionID else {
                throw SecureRelayError.sessionMismatch
            }
            if let last = credential.lastReceivedSequence, sequence <= last {
                throw SecureRelayError.replayedMessage
            }
            credentials[peerID] = credential.updating(lastReceivedSequence: .some(sequence))
        }
    }
}

public enum KeychainRelayError: Error, Equatable {
    case invalidCredential
    case operationFailed(OSStatus)
}

extension JSONDecoder {
    static var persome: JSONDecoder {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        return decoder
    }
}
