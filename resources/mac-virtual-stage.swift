// mac-virtual-stage — host a multi-instance agent app OFF-SCREEN, so the agent operates its own
// fresh instance and the user's real screen never changes (true no-steal, no flicker).
//
// The "virtual_stage" half of Persome's no-steal staging (the other half is the SkyLight borrow path
// for single-instance apps). For apps the agent can run its OWN copy of — browsers, the dominant
// computer-use target — this creates an off-screen CGVirtualDisplay, spawns a DEDICATED isolated
// instance positioned on it, and holds the display alive while the actuator drives that window.
// Any window-raise the app does happens on the virtual display = invisible to the user.
//
// Spec: docs/superpowers/specs/2026-06-26-persome-no-steal-staging-virtual-display-design.md
//
//   mac-virtual-stage --app "Google Chrome" --url "https://meet.google.com/new" \
//                     [--profile persome-stage] [--width 1920] [--height 1080]
//
// Emits ONE JSON line on stdout once the staged window appears:
//   {"display_id":N,"bounds":[x,y,w,h],"app_pid":P,"window_id":W,"profile":"/tmp/..."}
// then stays alive (holding the display) until SIGTERM / SIGINT / stdin EOF, at which point it
// kills the spawned instance (scoped to its isolated profile dir) and releases the display.
//
// CGVirtualDisplay is a private CoreGraphics API driven via NSClassFromString + KVC (proven on
// macOS 26.5; keeps this a pure-Swift helper in build-daemon.sh's swiftc loop). If the classes
// don't resolve (old macOS / API change) it prints {"error":"no_virtual_display"} and exits 3 so
// the caller degrades to the SkyLight/borrow path.

import AppKit
import CoreGraphics
import Foundation

// MARK: - args

func argValue(_ name: String) -> String? {
    let a = CommandLine.arguments
    guard let i = a.firstIndex(of: name), i + 1 < a.count else { return nil }
    return a[i + 1]
}

let appName = argValue("--app") ?? "Google Chrome"
let url = argValue("--url") ?? "about:blank"
let profileName = argValue("--profile") ?? "persome-stage"
let width = UInt32(argValue("--width") ?? "1920") ?? 1920
let height = UInt32(argValue("--height") ?? "1080") ?? 1080

func die(_ obj: [String: Any], code: Int32) -> Never {
    if let d = try? JSONSerialization.data(withJSONObject: obj), let s = String(data: d, encoding: .utf8) {
        FileHandle.standardError.write((s + "\n").data(using: .utf8)!)
    }
    exit(code)
}

func emit(_ obj: [String: Any]) {
    if let d = try? JSONSerialization.data(withJSONObject: obj), let s = String(data: d, encoding: .utf8) {
        FileHandle.standardOutput.write((s + "\n").data(using: .utf8)!)
    }
}

// MARK: - CGVirtualDisplay via KVC (private API)

func makeObj(_ cls: String) -> NSObject? {
    (NSClassFromString(cls) as? NSObject.Type)?.init()
}

/// Strong ref to the display object — the virtual display only lives while this is retained.
var heldDisplay: NSObject?

func createVirtualDisplay(_ w: UInt32, _ h: UInt32) -> UInt32? {
    guard let desc = makeObj("CGVirtualDisplayDescriptor"),
          let dispClass = NSClassFromString("CGVirtualDisplay") as? NSObject.Type,
          let settings = makeObj("CGVirtualDisplaySettings"),
          let modeClass = NSClassFromString("CGVirtualDisplayMode") as? NSObject.Type
    else { return nil }

    desc.setValue("Persome Agent Stage", forKey: "name")
    desc.setValue(w, forKey: "maxPixelsWide")
    desc.setValue(h, forKey: "maxPixelsHigh")
    desc.setValue(NSValue(size: CGSize(width: 600, height: 340)), forKey: "sizeInMillimeters")
    desc.setValue(UInt32(0x4D45), forKey: "productID")
    desc.setValue(UInt32(0x6E73), forKey: "vendorID")
    desc.setValue(UInt32(arc4random() & 0xFFFF), forKey: "serialNum")
    desc.setValue(DispatchQueue.main, forKey: "queue")

    let disp = dispClass.perform(NSSelectorFromString("alloc")).takeUnretainedValue()
        .perform(NSSelectorFromString("initWithDescriptor:"), with: desc).takeUnretainedValue() as! NSObject

    // CGVirtualDisplayMode initWithWidth:height:refreshRate: (3 args → runtime IMP)
    let modeAlloc = modeClass.perform(NSSelectorFromString("alloc")).takeUnretainedValue() as AnyObject
    typealias InitMode = @convention(c) (AnyObject, Selector, UInt32, UInt32, Double) -> Unmanaged<AnyObject>
    let sel = NSSelectorFromString("initWithWidth:height:refreshRate:")
    guard let imp = modeAlloc.method(for: sel) else { return nil }
    let mode = unsafeBitCast(imp, to: InitMode.self)(modeAlloc, sel, w, h, 60.0).takeUnretainedValue()

    settings.setValue(UInt32(0), forKey: "hiDPI")
    settings.setValue([mode], forKey: "modes")

    let ok = disp.perform(NSSelectorFromString("applySettings:"), with: settings) != nil
    let did = (disp.value(forKey: "displayID") as? UInt32) ?? 0
    guard ok, did != 0 else { return nil }
    heldDisplay = disp  // keep alive
    return did
}

// MARK: - window discovery

/// The spawned app's frontmost layer-0 window whose bounds sit on `displayBounds`. Returns
/// (window_id, owner_pid, rect) once it appears.
func stagedWindow(appMatch: String, displayBounds: CGRect) -> (UInt32, pid_t, CGRect)? {
    guard let list = CGWindowListCopyWindowInfo(
        [.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]]
    else { return nil }
    for w in list {
        guard let layer = w[kCGWindowLayer as String] as? Int, layer == 0,
              let owner = w[kCGWindowOwnerName as String] as? String, owner.contains(appMatch),
              let pid = w[kCGWindowOwnerPID as String] as? pid_t,
              let num = w[kCGWindowNumber as String] as? UInt32,
              let bd = w[kCGWindowBounds as String] as? [String: Any] else { continue }
        var r = CGRect.zero
        CGRectMakeWithDictionaryRepresentation(bd as CFDictionary, &r)
        if r.width < 40 || r.height < 40 { continue }
        if r.minX >= displayBounds.minX - 1 { return (num, pid, r) }  // on the virtual display
    }
    return nil
}

// MARK: - main

// distinct, scoped profile dir so we NEVER touch the user's real instance + can reap by path
let profileDir = NSTemporaryDirectory() + "persome-vstage-" + profileName + "-" + String(ProcessInfo.processInfo.processIdentifier)

guard let did = createVirtualDisplay(width, height) else {
    die(["error": "no_virtual_display"], code: 3)
}
RunLoop.current.run(until: Date(timeIntervalSinceNow: 0.4))
let vb = CGDisplayBounds(did)

// spawn the dedicated instance, positioned on the virtual display
let spawn = Process()
spawn.launchPath = "/usr/bin/open"
spawn.arguments = [
    "-g", "-n", "-a", appName, "--args",
    "--user-data-dir=" + profileDir, "--no-first-run", "--no-default-browser-check",
    "--window-position=\(Int(vb.minX) + 40),\(Int(vb.minY) + 40)",
    "--window-size=\(Int(vb.width) - 80),\(Int(vb.height) - 120)",
    "--new-window", url,
]
do { try spawn.run() } catch { die(["error": "spawn_failed", "detail": "\(error)"], code: 4) }

// teardown: kill the scoped profile instance + release the display
func teardown() {
    let k = Process()
    k.launchPath = "/usr/bin/pkill"
    k.arguments = ["-9", "-f", profileDir]
    try? k.run(); k.waitUntilExit()
    heldDisplay = nil
    exit(0)
}
signal(SIGTERM) { _ in teardown() }
signal(SIGINT) { _ in teardown() }
// stdin EOF (daemon closed the pipe) → teardown
DispatchQueue.global().async {
    let _ = FileHandle.standardInput.readDataToEndOfFile()
    teardown()
}

// poll up to ~12s for the staged window, then emit its identity
var emitted = false
for _ in 0..<120 {
    RunLoop.current.run(until: Date(timeIntervalSinceNow: 0.1))
    if let (wid, pid, r) = stagedWindow(appMatch: appName, displayBounds: vb) {
        emit([
            "display_id": did,
            "bounds": [Int(vb.minX), Int(vb.minY), Int(vb.width), Int(vb.height)],
            "app_pid": pid,
            "window_id": wid,
            "window_bounds": [Int(r.minX), Int(r.minY), Int(r.width), Int(r.height)],
            "profile": profileDir,
        ])
        emitted = true
        break
    }
}
if !emitted {
    // window never showed — emit the display anyway so the caller can decide
    emit(["display_id": did, "bounds": [Int(vb.minX), Int(vb.minY), Int(vb.width), Int(vb.height)],
          "profile": profileDir, "warning": "window_not_found"])
}

// hold the display until signaled
RunLoop.current.run()
