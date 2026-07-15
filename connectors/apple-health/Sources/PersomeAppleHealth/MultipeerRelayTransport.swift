@preconcurrency import MultipeerConnectivity
import Foundation

public enum RelayPeerState: String, Sendable {
    case connected
    case connecting
    case notConnected
}

public enum RelayTransportError: Error, Equatable {
    case ambiguousPeer
    case frameTooLarge
    case invalidFrame
    case peerNotFound
    case requestAlreadyPending
    case timedOut
    case unexpectedResponse
}

public struct RelayProtocolFailure: Codable, Sendable, Equatable {
    public let code: String

    public init(code: String) {
        self.code = code
    }
}

public enum RelayWireFrame: Codable, Sendable, Equatable {
    case clientHello(RelayClientHello)
    case serverHello(RelayServerHello)
    case clientConfirmation(RelayClientConfirmation)
    case pairingComplete(RelayPairingComplete)
    case envelope(RelayEnvelope)
    case failure(RelayProtocolFailure)
}

public enum RelayFrameCodec {
    /// A 2 MiB Runtime request expands under AES-GCM and JSON base64. Keep the
    /// transport ceiling explicit and only large enough for that bounded case.
    public static let maximumFrameBytes = 3 * 1024 * 1024

    public static func encode(_ frame: RelayWireFrame) throws -> Data {
        let data = try JSONEncoder.persome.encode(frame)
        guard data.count <= maximumFrameBytes else { throw RelayTransportError.frameTooLarge }
        return data
    }

    public static func decode(_ data: Data) throws -> RelayWireFrame {
        guard data.count <= maximumFrameBytes else { throw RelayTransportError.frameTooLarge }
        do {
            return try JSONDecoder.persome.decode(RelayWireFrame.self, from: data)
        } catch {
            throw RelayTransportError.invalidFrame
        }
    }
}

public protocol RelayRequestChannel: Sendable {
    func request(_ frame: RelayWireFrame, to peerID: String) async throws -> RelayWireFrame
}

/// Multipeer provides encrypted local discovery/transport; the relay protocol
/// still authenticates the paired endpoints and encrypts payloads end to end.
/// One request is in flight per peer, which preserves envelope order and keeps
/// reply routing deterministic.
public final class MultipeerRelayTransport: NSObject, RelayRequestChannel, @unchecked Sendable {
    public static let serviceType = "persome-health"

    public var onDiscoveredPeer: (@Sendable (String) -> Void)?
    public var onLostPeer: (@Sendable (String) -> Void)?
    public var onInvitation: (@Sendable (String) -> Void)?
    public var onStateChange: (@Sendable (String, RelayPeerState) -> Void)?
    public var onFrame: (@Sendable (String, RelayWireFrame) -> Void)?
    public var onError: (@Sendable (Error) -> Void)?

    private let localPeer: MCPeerID
    private let session: MCSession
    private let advertiser: MCNearbyServiceAdvertiser
    private let browser: MCNearbyServiceBrowser
    private let lock = NSLock()
    private var discovered: [MCPeerID] = []
    private var invitations: [MCPeerID: (Bool, MCSession?) -> Void] = [:]
    private struct PendingRequest {
        let token: UUID
        let continuation: CheckedContinuation<RelayWireFrame, any Error>
    }

    private var pending: [String: PendingRequest] = [:]

    public init(displayName: String) {
        localPeer = MCPeerID(displayName: displayName)
        session = MCSession(
            peer: localPeer,
            securityIdentity: nil,
            encryptionPreference: .required
        )
        advertiser = MCNearbyServiceAdvertiser(
            peer: localPeer,
            discoveryInfo: ["protocol": "2"],
            serviceType: Self.serviceType
        )
        browser = MCNearbyServiceBrowser(peer: localPeer, serviceType: Self.serviceType)
        super.init()
        session.delegate = self
        advertiser.delegate = self
        browser.delegate = self
    }

    public func start() {
        advertiser.startAdvertisingPeer()
        browser.startBrowsingForPeers()
    }

    public func stop() {
        browser.stopBrowsingForPeers()
        advertiser.stopAdvertisingPeer()
        session.disconnect()
        let state = lock.withLock { () -> (
            invitations: [(Bool, MCSession?) -> Void],
            pending: [CheckedContinuation<RelayWireFrame, any Error>]
        ) in
            defer {
                invitations.removeAll()
                pending.removeAll()
            }
            return (Array(invitations.values), pending.values.map(\.continuation))
        }
        for handler in state.invitations { handler(false, nil) }
        for continuation in state.pending {
            continuation.resume(throwing: RelayTransportError.peerNotFound)
        }
    }

    public func invite(displayName: String, timeout: TimeInterval = 30) throws {
        let matches = lock.withLock { discovered.filter { $0.displayName == displayName } }
        guard !matches.isEmpty else { throw RelayTransportError.peerNotFound }
        guard matches.count == 1, let peer = matches.first else {
            throw RelayTransportError.ambiguousPeer
        }
        browser.invitePeer(peer, to: session, withContext: nil, timeout: timeout)
    }

    public func respondToInvitation(from displayName: String, accept: Bool) throws {
        let matches = lock.withLock {
            invitations.keys.filter { $0.displayName == displayName }
        }
        guard !matches.isEmpty else { throw RelayTransportError.peerNotFound }
        guard matches.count == 1, let peer = matches.first else {
            throw RelayTransportError.ambiguousPeer
        }
        let handler = lock.withLock { invitations.removeValue(forKey: peer) }
        handler?(accept, accept ? session : nil)
    }

    public func send(_ frame: RelayWireFrame, to displayName: String) throws {
        let matches = session.connectedPeers.filter { $0.displayName == displayName }
        guard !matches.isEmpty else { throw RelayTransportError.peerNotFound }
        guard matches.count == 1, let peer = matches.first else {
            throw RelayTransportError.ambiguousPeer
        }
        try session.send(RelayFrameCodec.encode(frame), toPeers: [peer], with: .reliable)
    }

    public func request(
        _ frame: RelayWireFrame,
        to peerID: String
    ) async throws -> RelayWireFrame {
        try await withCheckedThrowingContinuation { continuation in
            let token = UUID()
            let accepted = lock.withLock { () -> Bool in
                guard pending[peerID] == nil else { return false }
                pending[peerID] = PendingRequest(token: token, continuation: continuation)
                return true
            }
            guard accepted else {
                continuation.resume(throwing: RelayTransportError.requestAlreadyPending)
                return
            }
            do {
                try send(frame, to: peerID)
                Task { [weak self] in
                    try? await Task.sleep(for: .seconds(30))
                    self?.expireRequest(peerID: peerID, token: token)
                }
            } catch {
                let waiting = lock.withLock { pending.removeValue(forKey: peerID) }
                waiting?.continuation.resume(throwing: error)
            }
        }
    }

    private func expireRequest(peerID: String, token: UUID) {
        let waiting = lock.withLock { () -> PendingRequest? in
            guard pending[peerID]?.token == token else { return nil }
            return pending.removeValue(forKey: peerID)
        }
        waiting?.continuation.resume(throwing: RelayTransportError.timedOut)
    }

    deinit {
        browser.stopBrowsingForPeers()
        advertiser.stopAdvertisingPeer()
        session.disconnect()
    }
}

extension MultipeerRelayTransport: MCNearbyServiceAdvertiserDelegate {
    public func advertiser(
        _: MCNearbyServiceAdvertiser,
        didReceiveInvitationFromPeer peerID: MCPeerID,
        withContext _: Data?,
        invitationHandler: @escaping (Bool, MCSession?) -> Void
    ) {
        lock.withLock { invitations[peerID] = invitationHandler }
        onInvitation?(peerID.displayName)
    }

    public func advertiser(
        _: MCNearbyServiceAdvertiser,
        didNotStartAdvertisingPeer error: any Error
    ) {
        onError?(error)
    }
}

extension MultipeerRelayTransport: MCNearbyServiceBrowserDelegate {
    public func browser(
        _: MCNearbyServiceBrowser,
        foundPeer peerID: MCPeerID,
        withDiscoveryInfo info: [String: String]?
    ) {
        guard info?["protocol"] == "2" else { return }
        lock.withLock {
            if !discovered.contains(peerID) { discovered.append(peerID) }
        }
        onDiscoveredPeer?(peerID.displayName)
    }

    public func browser(_: MCNearbyServiceBrowser, lostPeer peerID: MCPeerID) {
        lock.withLock { discovered.removeAll { $0 == peerID } }
        onLostPeer?(peerID.displayName)
    }

    public func browser(_: MCNearbyServiceBrowser, didNotStartBrowsingForPeers error: any Error) {
        onError?(error)
    }
}

extension MultipeerRelayTransport: MCSessionDelegate {
    public func session(_: MCSession, peer peerID: MCPeerID, didChange state: MCSessionState) {
        let relayState: RelayPeerState = switch state {
        case .connected: .connected
        case .connecting: .connecting
        case .notConnected: .notConnected
        @unknown default: .notConnected
        }
        if state == .notConnected {
            let waiting = lock.withLock { pending.removeValue(forKey: peerID.displayName) }
            waiting?.continuation.resume(throwing: RelayTransportError.peerNotFound)
        }
        onStateChange?(peerID.displayName, relayState)
    }

    public func session(_: MCSession, didReceive data: Data, fromPeer peerID: MCPeerID) {
        do {
            let frame = try RelayFrameCodec.decode(data)
            let waiting = lock.withLock { pending.removeValue(forKey: peerID.displayName) }
            if let waiting {
                waiting.continuation.resume(returning: frame)
            } else {
                onFrame?(peerID.displayName, frame)
            }
        } catch {
            onError?(error)
        }
    }

    public func session(
        _: MCSession,
        didReceive _: InputStream,
        withName _: String,
        fromPeer _: MCPeerID
    ) {}

    public func session(
        _: MCSession,
        didStartReceivingResourceWithName _: String,
        fromPeer _: MCPeerID,
        with _: Progress
    ) {}

    public func session(
        _: MCSession,
        didFinishReceivingResourceWithName _: String,
        fromPeer _: MCPeerID,
        at _: URL?,
        withError _: (any Error)?
    ) {}
}
