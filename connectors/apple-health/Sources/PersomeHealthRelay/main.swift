import Foundation
import PersomeAppleHealth

enum RelayHostCLIError: Error {
    case invalidRuntimeURL
    case missingBearerToken
}

@main
struct PersomeHealthRelayCLI {
    static func main() async throws {
        let environment = ProcessInfo.processInfo.environment
        guard let token = environment["PERSOME_LOCAL_API_TOKEN"], !token.isEmpty else {
            throw RelayHostCLIError.missingBearerToken
        }
        guard let runtimeURL = URL(
            string: environment["PERSOME_RUNTIME_URL"] ?? "http://127.0.0.1:8742"
        ) else {
            throw RelayHostCLIError.invalidRuntimeURL
        }

        let peerID = environment["PERSOME_RELAY_NAME"] ?? "Persome Mac"
        let store = KeychainRelayCredentialStore()
        let forwarder = try LoopbackHealthRuntimeForwarder(
            runtimeURL: runtimeURL,
            bearerToken: token
        )
        let host = MacHealthRelayHost(
            peerID: peerID,
            credentialStore: store,
            forwarder: forwarder
        )
        let pairingCode = String(format: "%06d", Int.random(in: 0 ... 999_999))
        let offer = try await host.beginPairing(pairingCode: pairingCode)
        let payload = try RelayPairingOfferCodec.encode(offer).base64EncodedString()

        let transport = MultipeerRelayTransport(displayName: peerID)
        let adapter = MultipeerHealthRelayHostAdapter(transport: transport, host: host)
        transport.onInvitation = { [weak transport] phonePeerID in
            do {
                // The owner explicitly launched this five-minute pairing window;
                // transcript authentication still requires its QR secret.
                try transport?.respondToInvitation(from: phonePeerID, accept: true)
            } catch {
                transport?.onError?(error)
            }
        }
        transport.onError = { error in
            FileHandle.standardError.write(Data("relay error: \(error)\n".utf8))
        }
        transport.start()

        print("Persome Apple Health relay is listening as \(peerID).")
        print("Pairing code: \(pairingCode)")
        print("Pairing payload (base64): \(payload)")
        print("The payload expires in five minutes; existing paired sessions remain resumable.")

        defer {
            transport.stop()
            withExtendedLifetime(adapter) {}
        }
        while !Task.isCancelled {
            try? await Task.sleep(for: .seconds(60))
        }
    }
}
