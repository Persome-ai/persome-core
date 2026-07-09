// Lists the bundle ids of every app registered with macOS LaunchServices to
// open http/https URLs, one per line on stdout. Used by the daemon's browser
// detector (capture/browser_detect.py) to classify ANY frontmost app as a web
// browser WITHOUT a hardcoded allowlist: only real browsers register as
// http(s) handlers (Tabbit, Arc, Chrome, Edge, …), while Electron apps (cmux,
// VSCode, Feishu, Claude) and native apps that merely embed an AXWebArea are
// correctly excluded.
//
// Pure read-only LaunchServices query — no entitlements, no AX/Screen-Recording
// permission needed. Compiled on demand like the other _bundled Swift helpers.
import AppKit
import Foundation

let ws = NSWorkspace.shared
var seen = Set<String>()
for scheme in ["https", "http"] {
    guard let probe = URL(string: "\(scheme)://example.com") else { continue }
    // urlsForApplications(toOpen:) is macOS 12+ (deployment target is 13).
    for appURL in ws.urlsForApplications(toOpen: probe) {
        guard let id = Bundle(url: appURL)?.bundleIdentifier, !id.isEmpty else { continue }
        if seen.insert(id).inserted {
            print(id)
        }
    }
}
