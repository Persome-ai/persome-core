# Persome Apple Health Connector

This Swift package is the iPhone-side bridge from Apple HealthKit (including
Apple Watch observations synchronized to the phone) to the owner-local Persome
Runtime. It requests read-only access, performs anchored incremental queries,
normalizes samples, and uploads bounded change pages through a
`HealthEventUploader`. Each page contains additions/corrections and HealthKit
deletion receipts; its anchor is persisted only after the entire page succeeds.

## Embed in an iPhone app

1. Add this directory as a local Swift package in Xcode.
2. Enable the HealthKit capability for the app target.
3. Add `NSHealthShareUsageDescription` to the app's `Info.plist`, explaining
   that selected observations are sent only to the owner's local Persome Runtime.
4. Create the connector with an uploader implemented by the host app, then
   authorize and sync:

```swift
let connector = AppleHealthConnector(client: secureRelayClient)
try await connector.requestAuthorization()
let result = try await connector.sync()
```

The Runtime is loopback-only. `PersomeHealthClient` therefore accepts only
`localhost`, `127.0.0.1`, or `::1` and is intended for the Mac relay and tests.
Never place the owner bearer on the phone or expose the Runtime over LAN.

The first slice reads steps, heart rate, resting heart rate, active energy,
sleep analysis, and workouts. Anchored queries use 500-operation pages rather
than materializing an unlimited history. Interrupted syncs safely replay the
last unacknowledged page and rely on server-side idempotency.

## Run the Mac relay

The package includes a real Mac relay host. It owns the Runtime bearer, accepts
encrypted Multipeer frames, authenticates and decrypts them, and forwards only
to the loopback `POST /health-events/import` route:

```bash
cd connectors/apple-health
set -a
source "${PERSOME_ROOT:-$HOME/.persome}/env"
set +a
swift run persome-health-relay
```

The command prints a six-digit comparison code and a short-lived base64 pairing
payload suitable for a QR code. `PERSOME_RUNTIME_URL` may override the default
`http://127.0.0.1:8742`, but non-loopback URLs are rejected. The long-lived
`PERSOME_LOCAL_API_TOKEN` is used only by `LoopbackHealthRuntimeForwarder`; it is
never encoded into a pairing offer, credential, relay request, or phone client.

## Pair and upload from the phone

The iPhone app scans the host payload, connects to the peer named by the offer,
and runs both confirmed handshake flights over `MultipeerRelayTransport`:

```swift
let offerData = Data(base64Encoded: scannedPayload)!
let offer = try RelayPairingOfferCodec.decode(offerData)
let transport = MultipeerRelayTransport(displayName: phonePeerID)
transport.start()

// Use onDiscoveredPeer/onStateChange to invite offer.responderPeerID and wait
// for .connected. Ask the owner to confirm the code displayed by the Mac,
// then run the authenticated handshake on that same channel.
try await SecureHealthRelayPairer.pair(
    offer: offer,
    confirmedPairingCode: userConfirmedCode,
    ownPeerID: phonePeerID,
    credentialStore: KeychainRelayCredentialStore(),
    channel: transport
)
let secureRelayClient = SecureHealthRelayClient(
    macPeerID: offer.responderPeerID,
    credentialStore: KeychainRelayCredentialStore(),
    channel: transport
)
let connector = AppleHealthConnector(client: secureRelayClient)
```

The QR payload carries a random 32-byte pairing secret. The six-digit code is a
human comparison value, not the session's entropy. Each pairing also creates
fresh P-256 keys, nonces, and a session ID. ECDH + HKDF-SHA256 binds those values
to a canonical transcript; the Mac and phone must present separate HMAC key
confirmations before either stores a session.

Health payloads and Runtime receipts use directional AES-256-GCM keys. The
session ID, direction, version, and sequence are authenticated. Send counters
are persisted before encryption, and receive watermarks are persisted after
authentication but before Runtime forwarding, preventing nonce reuse and
replay across process restarts. Credentials expire after 30 days and are stored
as `AfterFirstUnlockThisDeviceOnly`, non-synchronizing Keychain items. A new
pairing replaces the old session for that peer.

Multipeer itself uses required transport encryption, while the relay protocol
provides endpoint authentication and end-to-end payload protection. Frames are
bounded to 3 MiB for the Runtime's 2 MiB plaintext ceiling, allow one in-flight
request per peer, and time out after 30 seconds.

iOS host apps must add `NSLocalNetworkUsageDescription` and
`_persome-health._tcp` under `NSBonjourServices` in `Info.plist`.

The deterministic protocol, restart, replay, bound, and loopback-forwarding
paths run under `swift test`. HealthKit authorization, entitlements, real
Multipeer discovery, background execution, and a scanned-QR UI still require
physical iPhone/Apple Watch plus Xcode validation by the embedding app.
