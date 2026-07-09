// mac-ax-actuator — the "hands" half of Persome's AX-first actuation layer.
//
// Read-only `mac-ax-helper` produces capture text; THIS binary additionally PERFORMS actions
// (a distinct, write-capable trust profile, kept a separate binary for capability separation +
// independent signing/TCC). It targets elements by a re-resolvable AX PATH id (not raw coords),
// validates the element's label on re-resolve (UI changed ⇒ fail closed, never a wrong-element
// misfire), and — like mediar-ai/mcp-server-macos-use's `showDiff` — returns a before/after AX
// DIFF as the action's feedback (what changed = proof the action landed).
//
// Spec/plan: docs/superpowers/{specs,plans}/2026-06-25-persome-actuation-layer-*.md
//
// Subcommands (JSON on stdout):
//   mac-ax-actuator snapshot [--pid N | --app NAME]
//   mac-ax-actuator act --pid N --id <path-id> --verb press
//   mac-ax-actuator act --pid N --id <path-id> --verb setvalue --text "..."
//   mac-ax-actuator act --pid N --verb key --keys "cmd+v"
//   mac-ax-actuator trust            (print AX-trust status, exit 0/1)

import AppKit
import ApplicationServices
import Foundation

// MARK: - AX read helpers

func axCopy(_ el: AXUIElement, _ attr: String) -> CFTypeRef? {
    var ref: CFTypeRef?
    return AXUIElementCopyAttributeValue(el, attr as CFString, &ref) == .success ? ref : nil
}

func axStr(_ el: AXUIElement, _ attr: String) -> String? {
    guard let v = axCopy(el, attr) else { return nil }
    if let s = v as? String { return s }
    if let n = v as? NSNumber { return n.stringValue }
    return nil
}

func axChildren(_ el: AXUIElement) -> [AXUIElement] {
    guard let v = axCopy(el, kAXChildrenAttribute as String), let arr = v as? [AXUIElement] else { return [] }
    return arr
}

func axActions(_ el: AXUIElement) -> [String] {
    var names: CFArray?
    guard AXUIElementCopyActionNames(el, &names) == .success, let a = names as? [String] else { return [] }
    return a
}

func axBoolAttr(_ el: AXUIElement, _ attr: String) -> Bool {
    guard let v = axCopy(el, attr), let n = v as? NSNumber else { return false }
    return n.boolValue
}

/// Screen-space [x, y, w, h] from kAXPosition + kAXSize, or nil.
func axFrame(_ el: AXUIElement) -> [Double]? {
    guard let pRef = axCopy(el, kAXPositionAttribute as String),
          let sRef = axCopy(el, kAXSizeAttribute as String) else { return nil }
    var pt = CGPoint.zero, sz = CGSize.zero
    // swiftlint:disable force_cast
    AXValueGetValue(pRef as! AXValue, .cgPoint, &pt)
    AXValueGetValue(sRef as! AXValue, .cgSize, &sz)
    // swiftlint:enable force_cast
    let f = [Double(pt.x), Double(pt.y), Double(sz.width), Double(sz.height)]
    // Some apps (e.g. System Settings panes) expose elements with a non-finite coordinate/size.
    // JSON can't encode inf/nan, so emitting such a bbox makes NSJSONSerialization throw and crashes
    // the WHOLE snapshot (→ 0 elements). Drop the bbox for those elements instead of dying.
    return f.allSatisfy { $0.isFinite } ? f : nil
}

func label(_ el: AXUIElement) -> String {
    axStr(el, kAXTitleAttribute as String)
        ?? axStr(el, kAXDescriptionAttribute as String)
        ?? axStr(el, "AXLabel")
        ?? ""
}

func role(_ el: AXUIElement) -> String { axStr(el, kAXRoleAttribute as String) ?? "AXUnknown" }

func valueString(_ el: AXUIElement) -> String? {
    guard let v = axCopy(el, kAXValueAttribute as String) else { return nil }
    if let s = v as? String { return s }
    if let n = v as? NSNumber { return n.stringValue }
    return nil
}

// MARK: - Path id (re-resolvable, label-validated)

/// A short, stable hash of a label for the validation suffix.
func labelHash(_ s: String) -> String {
    var h: UInt64 = 1469598103934665603  // FNV-1a
    for b in s.utf8 { h = (h ^ UInt64(b)) &* 1099511628211 }
    return String(format: "%08x", UInt32(truncatingIfNeeded: h))
}

/// Encode a child-index path + label hash → `base64("i0.i1...#hash")`.
func encodeId(_ path: [Int], _ lbl: String) -> String {
    let raw = path.map(String.init).joined(separator: ".") + "#" + labelHash(lbl)
    return Data(raw.utf8).base64EncodedString()
}

/// Decode → (path, hash). Returns nil on malformed input.
func decodeId(_ id: String) -> (path: [Int], hash: String)? {
    guard let data = Data(base64Encoded: id), let raw = String(data: data, encoding: .utf8) else { return nil }
    let parts = raw.split(separator: "#", maxSplits: 1)
    guard parts.count == 2 else { return nil }
    let path = parts[0].isEmpty ? [] : parts[0].split(separator: ".").compactMap { Int($0) }
    return (path, String(parts[1]))
}

/// Re-walk from `appEl` along the child-index path; validate the label hash. Returns the element or
/// nil with a reason (`not_found` / `stale`).
func resolve(_ appEl: AXUIElement, _ id: String) -> (el: AXUIElement?, error: String?) {
    guard let (path, hash) = decodeId(id) else { return (nil, "bad_id") }
    var cur = appEl
    for idx in path {
        let kids = axChildren(cur)
        guard idx >= 0, idx < kids.count else { return (nil, "not_found") }
        cur = kids[idx]
    }
    if labelHash(label(cur)) != hash { return (nil, "stale") }
    return (cur, nil)
}

// MARK: - Traversal → elements + state map

struct Walked {
    var elements: [[String: Any]] = []          // actionable elements (for snapshot)
    var state: [String: [String: String]] = [:] // id → {role,label,value} (for diff)
}

let actionableRoles: Set<String> = [
    "AXButton", "AXMenuItem", "AXMenuBarItem", "AXCheckBox", "AXRadioButton",
    "AXTextField", "AXTextArea", "AXComboBox", "AXPopUpButton", "AXLink", "AXSlider",
]

func walk(_ el: AXUIElement, path: [Int], depth: Int, maxDepth: Int, into w: inout Walked) {
    let r = role(el)
    let lbl = label(el)
    let id = encodeId(path, lbl)
    let acts = axActions(el)
    let editable = (r == "AXTextField" || r == "AXTextArea" || r == "AXComboBox")
    let val = valueString(el)
    // Emit anything the agent can act on OR READ: an element with a value (a text field, a result
    // display like Calculator's "编辑字段" child, any AXStaticText carrying content) is surfaced even
    // when it has no actions — otherwise the agent can't read on-screen values and has to guess them.
    let isActionable = !acts.isEmpty || actionableRoles.contains(r)
    if isActionable || val != nil {
        var e: [String: Any] = [
            "id": id, "role": r, "label": lbl, "actions": acts,
            "enabled": !axBoolAttr(el, "AXDisabledAttribute") && (axCopy(el, "AXEnabled").map { ($0 as? NSNumber)?.boolValue ?? true } ?? true),
            "editable": editable,
        ]
        if let f = axFrame(el) { e["bbox"] = f }
        if let v = val { e["value"] = v }
        w.elements.append(e)
    }
    // State map for the diff: any element carrying a value or a non-empty label.
    if let v = val {
        w.state[id] = ["role": r, "label": lbl, "value": v]
    }

    guard depth < maxDepth else { return }
    let kids = axChildren(el)
    for (i, child) in kids.enumerated() {
        walk(child, path: path + [i], depth: depth + 1, maxDepth: maxDepth, into: &w)
    }
}

func snapshotApp(pid: pid_t, maxDepth: Int) -> Walked {
    let appEl = AXUIElementCreateApplication(pid)
    AXUIElementSetAttributeValue(appEl, "AXManualAccessibility" as CFString, kCFBooleanTrue) // force Electron AX
    var w = Walked()
    walk(appEl, path: [], depth: 0, maxDepth: maxDepth, into: &w)
    // Cold AXManualAccessibility race: Chromium/Electron populate their out-of-process web-AX tree
    // asynchronously after the attribute is set, so a brand-new one-shot process that walks
    // immediately can see an (almost) empty tree (raw_elements=0, ok=true). A long-running watcher
    // that holds the app force-AX'd hides this — but a cold actuator invocation must not. Retry with
    // a short bounded backoff until the tree populates, so cold snapshots are reliable on their own.
    var tries = 0
    while w.elements.count <= 3 && tries < 4 {
        usleep(200_000) // 0.2s — bounded (≤0.8s) so a genuinely-empty surface doesn't stall
        w = Walked()
        walk(appEl, path: [], depth: 0, maxDepth: maxDepth, into: &w)
        tries += 1
    }
    return w
}

// MARK: - before-state cache (skip the redundant snap_before)
//
// Each act snapshots BEFORE (for the diff) and AFTER. But the previous act/snapshot already captured
// the AFTER-state, which IS the current state at the start of the next act (nothing happened in
// between but the model's think time). So we cache the latest state per pid and reuse it as `before`
// (with `--cache-before`), halving an act's snapshot work. Bounded by file mtime so a stale cache
// (UI drifted while idle) is ignored — and the AFTER snapshot is always fresh, so the actionable
// elements the model acts on are never stale; only the (secondary) diff could miss an idle change.

func stateCachePath(_ pid: pid_t) -> String { "/tmp/persome-axcache-\(pid).json" }

func loadCachedState(_ pid: pid_t, maxAge: Double = 6.0) -> [String: [String: String]]? {
    let path = stateCachePath(pid)
    guard let attrs = try? FileManager.default.attributesOfItem(atPath: path),
          let mtime = attrs[.modificationDate] as? Date,
          Date().timeIntervalSince(mtime) < maxAge,
          let data = FileManager.default.contents(atPath: path),
          let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: [String: String]]
    else { return nil }
    return obj
}

func saveCachedState(_ pid: pid_t, _ state: [String: [String: String]]) {
    if let data = try? JSONSerialization.data(withJSONObject: state) {
        try? data.write(to: URL(fileURLWithPath: stateCachePath(pid)))
    }
}

/// Diff two state maps → list of {id, role, label, before, after, change}.
func diffState(_ before: [String: [String: String]], _ after: [String: [String: String]]) -> [[String: Any]] {
    var out: [[String: Any]] = []
    let keys = Set(before.keys).union(after.keys)
    for k in keys {
        let b = before[k], a = after[k]
        if b == nil, let a = a {
            out.append(["id": k, "role": a["role"] ?? "", "label": a["label"] ?? "", "after": a["value"] ?? "", "change": "appeared"])
        } else if a == nil, let b = b {
            out.append(["id": k, "role": b["role"] ?? "", "label": b["label"] ?? "", "before": b["value"] ?? "", "change": "disappeared"])
        } else if let b = b, let a = a, b["value"] != a["value"] {
            out.append(["id": k, "role": a["role"] ?? "", "label": a["label"] ?? "",
                        "before": b["value"] ?? "", "after": a["value"] ?? "", "change": "changed"])
        }
    }
    return out
}

// MARK: - Actions

func performVerb(_ el: AXUIElement, verb: String, text: String?) -> String? {
    switch verb {
    case "press":
        return AXUIElementPerformAction(el, kAXPressAction as CFString) == .success ? nil : "press_failed"
    case "setvalue":
        guard let t = text else { return "missing_text" }
        return AXUIElementSetAttributeValue(el, kAXValueAttribute as CFString, t as CFTypeRef) == .success ? nil : "setvalue_failed"
    case "confirm":
        return AXUIElementPerformAction(el, kAXConfirmAction as CFString) == .success ? nil : "confirm_failed"
    case "action":
        // Perform an ARBITRARY AX action the element advertises (AXIncrement / AXDecrement /
        // AXShowMenu / AXPick / AXRaise / …) — for steppers, date/number pickers, dropdowns and other
        // controls that do nothing on a plain AXPress. The action name (`text`) must be one of the
        // element's own actions (the `actions` list in its snapshot); otherwise the OS rejects it.
        guard let name = text, !name.isEmpty else { return "missing_action" }
        return AXUIElementPerformAction(el, name as CFString) == .success ? nil : "action_failed"
    default:
        return "unknown_verb"
    }
}

// MARK: - CGEvent keyboard / mouse (for Electron & co. where AXValue-set / AX-click don't take)

let _src = CGEventSource(stateID: .combinedSessionState)

/// When set (act --bg), CGEvents are delivered to THIS pid via `postToPid` instead of the global
/// HID tap — so a background actuation doesn't move the real cursor or steal focus from whatever the
/// user is doing. (AX press/setvalue never touch the cursor at all; this only covers the CGEvent
/// fallbacks — the pixel-button click + Unicode typing. Background delivery is app-dependent: some
/// apps only process events while frontmost, so AX stays the preferred no-steal path.)
var _postPid: pid_t?

func postEvent(_ ev: CGEvent?) {
    guard let ev = ev else { return }
    // Keyboard verbs go through here — post ONCE (double-posting would type every char twice). The
    // mouse-click recipe uses `postBoth` internally (idempotent for a click, not for keystrokes).
    if _bgMode == .skylight, let pid = _postPid { SkyLight.postSingle(ev, toPid: pid); return }
    if let pid = _postPid { ev.postToPid(pid) } else { ev.post(tap: .cghidEventTap) }
}

// MARK: - SkyLight background driver (no focus/cursor steal)
//
// The "background computer use" path: deliver synthesized input to a SPECIFIC pid via SkyLight's
// per-pid post channel + focus-without-raise, so the real cursor never moves and the user's front app
// never changes — yet a backgrounded Electron/Chromium target (Feishu, Chrome, VS Code) still accepts
// the click. Recipe ported from trycua/cua-driver (MIT) `Input/{SkyLightEventPost,FocusWithoutRaise,
// MouseInput}.swift`, verified reproducible on macOS 26.5 against real Chrome (front+cursor unchanged).
// Every symbol is runtime-resolved (`dlopen`/`dlsym`); if any is missing `available` is false and the
// caller degrades to the plain postToPid / foreground path. AX press/setvalue stay the preferred
// no-steal path; this covers the CGEvent verbs (coordinate click + keyboard) AX can't express.

enum BgMode { case none, postpid, skylight }
var _bgMode: BgMode = .none

enum SkyLight {
    typealias PostToPidFn = @convention(c) (pid_t, CGEvent) -> Void
    typealias SetIntFieldFn = @convention(c) (CGEvent, UInt32, Int64) -> Void
    typealias SetWinLocFn = @convention(c) (CGEvent, CGPoint) -> Void
    typealias PostRecFn = @convention(c) (UnsafeRawPointer, UnsafePointer<UInt8>) -> Int32
    typealias GetFrontFn = @convention(c) (UnsafeMutableRawPointer) -> Int32
    typealias ConnIDFn = @convention(c) () -> UInt32
    typealias GetWinOwnerFn = @convention(c) (UInt32, UInt32, UnsafeMutablePointer<UInt32>) -> Int32
    typealias GetConnPSNFn = @convention(c) (UInt32, UnsafeMutableRawPointer) -> Int32

    static let RTLD_DEFAULT = UnsafeMutableRawPointer(bitPattern: -2)
    static func sym<T>(_ n: String, _ t: T.Type) -> T? {
        guard let p = dlsym(RTLD_DEFAULT, n) else { return nil }
        return unsafeBitCast(p, to: T.self)
    }
    static let _open: Void = { _ = dlopen("/System/Library/PrivateFrameworks/SkyLight.framework/SkyLight", RTLD_LAZY) }()
    static let postToPidFn = sym("SLEventPostToPid", PostToPidFn.self)
    static let setIntFn = sym("SLEventSetIntegerValueField", SetIntFieldFn.self)
    static let postRecFn = sym("SLPSPostEventRecordTo", PostRecFn.self)
    static let getFrontFn = sym("_SLPSGetFrontProcess", GetFrontFn.self)
    static let connIDFn = sym("CGSMainConnectionID", ConnIDFn.self)
    static let winOwnerFn = sym("SLSGetWindowOwner", GetWinOwnerFn.self)
    static let connPSNFn = sym("SLSGetConnectionPSN", GetConnPSNFn.self)
    static let setWinLocFn = sym("CGEventSetWindowLocation", SetWinLocFn.self)
    typealias SetFrontFn = @convention(c) (UnsafeRawPointer, UInt32, UInt32) -> Int32
    static let setFrontFn = sym("SLPSSetFrontProcessWithOptions", SetFrontFn.self)

    /// Restore `psn` as the WindowServer-frontmost process WITHOUT raising windows (kCPSNoWindows
    /// 0x400) — used to undo the AppKit-active change that focus-without-raise leaves on the TARGET,
    /// so the user's app goes back to being frontmost (Electron apps otherwise stay active = steal).
    static func restoreFront(_ psn: [UInt32]) {
        guard let f = setFrontFn else { return }
        _ = psn.withUnsafeBytes { f($0.baseAddress!, 0, 0x400) }
    }

    /// All the SPIs needed for the no-steal click recipe resolve on this OS.
    static var available: Bool {
        _ = _open
        return postToPidFn != nil && setIntFn != nil && postRecFn != nil && getFrontFn != nil
            && connIDFn != nil && winOwnerFn != nil && connPSNFn != nil
    }
    static func probe() -> [String: Bool] {
        _ = _open
        return [
            "SLEventPostToPid": postToPidFn != nil, "SLEventSetIntegerValueField": setIntFn != nil,
            "SLPSPostEventRecordTo": postRecFn != nil, "_SLPSGetFrontProcess": getFrontFn != nil,
            "CGSMainConnectionID": connIDFn != nil, "SLSGetWindowOwner": winOwnerFn != nil,
            "SLSGetConnectionPSN": connPSNFn != nil, "CGEventSetWindowLocation": setWinLocFn != nil,
        ]
    }

    /// Post via both the SkyLight per-pid channel and the public `CGEvent.postToPid`; neither moves
    /// the real cursor. (cua-driver's `postBoth` — SkyLight lands on Chromium, postToPid on AppKit.)
    /// For MOUSE only — double-delivery is idempotent for a click but would type a keystroke twice.
    static func postBoth(_ ev: CGEvent, toPid pid: pid_t) {
        if let f = postToPidFn { f(pid, ev) }
        ev.postToPid(pid)
    }

    /// Single per-pid delivery — for keyboard, where `postBoth` would duplicate every character.
    static func postSingle(_ ev: CGEvent, toPid pid: pid_t) {
        if let f = postToPidFn { f(pid, ev) } else { ev.postToPid(pid) }
    }

    private static func psnForWindow(_ wid: UInt32) -> [UInt32]? {
        guard let owner = winOwnerFn, let psnFn = connPSNFn, let conn = connIDFn else { return nil }
        var psn = [UInt32](repeating: 0, count: 2)
        var ownerCid: UInt32 = 0
        guard owner(conn(), wid, &ownerCid) == 0 else { return nil }
        let ok = psn.withUnsafeMutableBytes { psnFn(ownerCid, $0.baseAddress!) == 0 }
        return ok ? psn : nil
    }

    /// Put `wid`'s app into AppKit-active input state without raising the window or switching Space.
    /// Returns the PREVIOUS front PSN (8 bytes as 2×UInt32) so the caller can restore it afterward —
    /// undoing the AppKit-active change so the user's app stays frontmost. nil on failure.
    static func focusWithoutRaise(wid: UInt32) -> [UInt32]? {
        guard let getFront = getFrontFn, let postRec = postRecFn else { return nil }
        var prev = [UInt32](repeating: 0, count: 2)
        guard prev.withUnsafeMutableBytes({ getFront($0.baseAddress!) == 0 }) else { return nil }
        guard let target = psnForWindow(wid) else { return nil }
        var buf = [UInt8](repeating: 0, count: 0xF8)
        buf[0x04] = 0xF8; buf[0x08] = 0x0D
        buf[0x3C] = UInt8(wid & 0xFF); buf[0x3D] = UInt8((wid >> 8) & 0xFF)
        buf[0x3E] = UInt8((wid >> 16) & 0xFF); buf[0x3F] = UInt8((wid >> 24) & 0xFF)
        buf[0x8A] = 0x02  // defocus previous front
        _ = prev.withUnsafeBytes { ps in buf.withUnsafeBufferPointer { postRec(ps.baseAddress!, $0.baseAddress!) } }
        buf[0x8A] = 0x01  // focus target
        _ = target.withUnsafeBytes { ps in buf.withUnsafeBufferPointer { postRec(ps.baseAddress!, $0.baseAddress!) } }
        return prev
    }

    private static let mainH = NSScreen.main?.frame.height ?? NSScreen.screens.first?.frame.height ?? 0
    private static func makeEvent(_ type: NSEvent.EventType, clickCount: Int, wid: UInt32) -> CGEvent? {
        // NSEvent-bridged construction — raw CGEvent mouse events are filtered at Chromium's renderer
        // IPC boundary; the AppKit bridge produces events Chromium accepts as trusted.
        NSEvent.mouseEvent(with: type, location: .zero, modifierFlags: [], timestamp: 0,
            windowNumber: Int(wid), context: nil, eventNumber: 0, clickCount: clickCount, pressure: 1.0)?.cgEvent
    }
    // Exact cua-driver `MouseInput.stamp` — location + button/subtype/clickState +
    // windowUnderMousePointer(+ThatCanHandle) + window-local point + SkyLight field 40 = pid. (No
    // f0/f51/f91/f92 — cua-driver doesn't stamp those in the click path.)
    private static func stamp(_ e: CGEvent, screen: CGPoint, winLocal: CGPoint, f0: Int64, pid: pid_t, wid: UInt32) {
        e.location = screen
        e.setIntegerValueField(.mouseEventButtonNumber, value: 0)
        e.setIntegerValueField(.mouseEventSubtype, value: 3)
        e.setIntegerValueField(.mouseEventClickState, value: 1)
        if wid != 0 {
            e.setIntegerValueField(.mouseEventWindowUnderMousePointer, value: Int64(wid))
            e.setIntegerValueField(.mouseEventWindowUnderMousePointerThatCanHandleThisEvent, value: Int64(wid))
        }
        if let f = setWinLocFn { f(e, winLocal) }
        if let f = setIntFn { f(e, 40, Int64(pid)) }
    }

    /// Background no-steal left-click at screen point (top-left origin), delivered to `pid`'s window
    /// `wid`. Returns false when the SPIs aren't available (caller degrades). Sequence: focus-without-
    /// raise → mouseMoved → off-screen (-1,-1) primer (opens Chromium's user-activation gate) → real
    /// down/up. window origin needed for the window-local point stamp.
    @discardableResult
    static func click(x: Double, y: Double, pid: pid_t, wid: UInt32, winOrigin: CGPoint) -> Bool {
        guard available, wid != 0 else { return false }
        let prevFront = focusWithoutRaise(wid: wid); usleep(50_000)
        defer { if let prev = prevFront { restoreFront(prev) } }   // undo the AppKit-active steal
        let target = CGPoint(x: x, y: y)
        let winLocal = CGPoint(x: x - winOrigin.x, y: y - winOrigin.y)
        let off = CGPoint(x: -1, y: -1)
        guard let move = makeEvent(.mouseMoved, clickCount: 0, wid: wid),
              let pd = makeEvent(.leftMouseDown, clickCount: 1, wid: wid),
              let pu = makeEvent(.leftMouseUp, clickCount: 1, wid: wid),
              let td = makeEvent(.leftMouseDown, clickCount: 1, wid: wid),
              let tu = makeEvent(.leftMouseUp, clickCount: 1, wid: wid) else { return false }
        stamp(move, screen: target, winLocal: winLocal, f0: 2, pid: pid, wid: wid)
        stamp(pd, screen: off, winLocal: off, f0: 1, pid: pid, wid: wid)
        stamp(pu, screen: off, winLocal: off, f0: 2, pid: pid, wid: wid)
        stamp(td, screen: target, winLocal: winLocal, f0: 3, pid: pid, wid: wid)
        stamp(tu, screen: target, winLocal: winLocal, f0: 3, pid: pid, wid: wid)
        // Post ONLY via SLEventPostToPid (the auth-signed SkyLight channel) — matching cua-driver's
        // primary left-click path. The public `CGEvent.postToPid` (postBoth's second leg) ACTIVATES
        // Electron apps (Feishu/Slack came to front), defeating no-steal; SkyLight-only stays background.
        func p(_ e: CGEvent) {
            e.timestamp = clock_gettime_nsec_np(CLOCK_UPTIME_RAW)
            if let f = postToPidFn { f(pid, e) } else { e.postToPid(pid) }
        }
        p(move); usleep(15_000)
        p(pd); usleep(1_000); p(pu); usleep(100_000)
        p(td); usleep(1_000); p(tu); usleep(150_000)
        return true
    }

    /// The frontmost on-screen window of `pid` (id + origin) for focus-without-raise + window-local pt.
    static func frontWindow(ofPid pid: pid_t) -> (wid: UInt32, origin: CGPoint)? {
        guard let arr = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] else { return nil }
        var best: (UInt32, CGPoint, Double)? = nil
        for w in arr {
            guard (w[kCGWindowOwnerPID as String] as? pid_t) == pid,
                  (w[kCGWindowLayer as String] as? Int) == 0,
                  let b = w[kCGWindowBounds as String] as? [String: Any],
                  let wd = b["Width"] as? Double, let ht = b["Height"] as? Double, wd > 100, ht > 100,
                  let num = w[kCGWindowNumber as String] as? Int else { continue }
            let area = wd * ht
            if best == nil || area > best!.2 {
                best = (UInt32(num), CGPoint(x: b["X"] as? Double ?? 0, y: b["Y"] as? Double ?? 0), area)
            }
        }
        return best.map { ($0.0, $0.1) }
    }
}

/// Named non-character keys → virtual keycode.
let keyCodes: [String: CGKeyCode] = [
    "return": 0x24, "enter": 0x24, "tab": 0x30, "space": 0x31, "delete": 0x33,
    "backspace": 0x33, "escape": 0x35, "esc": 0x35,
    "left": 0x7B, "right": 0x7C, "down": 0x7D, "up": 0x7E,
    "home": 0x73, "end": 0x77, "pageup": 0x74, "pagedown": 0x79,
    // full ANSI letter map — a partial map silently no-op'd combos like cmd+shift+n / cmd+alt+l /
    // cmd+shift+p (unknown_key returned BEFORE posting), so those shortcuts never fired at all.
    "a": 0x00, "b": 0x0B, "c": 0x08, "d": 0x02, "e": 0x0E, "f": 0x03, "g": 0x05,
    "h": 0x04, "i": 0x22, "j": 0x26, "k": 0x28, "l": 0x25, "m": 0x2E, "n": 0x2D,
    "o": 0x1F, "p": 0x23, "q": 0x0C, "r": 0x0F, "s": 0x01, "t": 0x11, "u": 0x20,
    "v": 0x09, "w": 0x0D, "x": 0x07, "y": 0x10, "z": 0x06,
    "0": 0x1D, "1": 0x12, "2": 0x13, "3": 0x14, "4": 0x15, "5": 0x17,
    "6": 0x16, "7": 0x1A, "8": 0x1C, "9": 0x19,
    "comma": 0x2B, "period": 0x2F, "slash": 0x2C, "backtick": 0x32,
    // operator/numpad keys (e.g. for Calculator keyboard entry) — numpad codes need no shift
    "+": 0x45, "plus": 0x45, "-": 0x1B, "minus": 0x1B, "*": 0x43, "asterisk": 0x43,
    "/": 0x4B, "divide": 0x4B, "=": 0x18, "equals": 0x18, ".": 0x2F,
]

let modFlags: [String: CGEventFlags] = [
    "cmd": .maskCommand, "command": .maskCommand, "ctrl": .maskControl, "control": .maskControl,
    "alt": .maskAlternate, "opt": .maskAlternate, "option": .maskAlternate, "shift": .maskShift,
]

/// Post a key combo like "enter", "cmd+k", "cmd+shift+a".
func postKeyCombo(_ combo: String) -> String? {
    let parts = combo.lowercased().split(separator: "+").map(String.init)
    guard let keyName = parts.last, let code = keyCodes[keyName] else { return "unknown_key" }
    var flags: CGEventFlags = []
    for m in parts.dropLast() { if let f = modFlags[m] { flags.insert(f) } }
    let down = CGEvent(keyboardEventSource: _src, virtualKey: code, keyDown: true)
    down?.flags = flags
    postEvent(down)
    let up = CGEvent(keyboardEventSource: _src, virtualKey: code, keyDown: false)
    up?.flags = flags
    postEvent(up)
    return nil
}

/// Type arbitrary Unicode (incl. Chinese) by posting per-character key events — bypasses keycode
/// maps and IME, so it lands the literal text into the focused field.
func typeUnicode(_ text: String) {
    for ch in text {
        let utf16 = Array(String(ch).utf16)
        for keyDown in [true, false] {
            let ev = CGEvent(keyboardEventSource: _src, virtualKey: 0, keyDown: keyDown)
            ev?.keyboardSetUnicodeString(stringLength: utf16.count, unicodeString: utf16)
            postEvent(ev)
        }
        usleep(4000)
    }
}

/// Left-click at a screen coordinate (top-left origin, as AX bbox uses) — focuses a field whose
/// element AX-click doesn't take (Electron). On the GLOBAL path (no --bg), the synthetic click moves
/// the real cursor; we snapshot the cursor first and warp it straight back, so the user sees a brief
/// flicker instead of the cursor being left parked elsewhere. (On the --bg/postToPid path the cursor
/// never moves, so there is nothing to restore.)
func clickAt(x: Double, y: Double) {
    // SkyLight no-steal path: deliver the click to the target window without moving the cursor or
    // raising the window. Falls through to the legacy path if the SPIs aren't available / no window.
    if _bgMode == .skylight, let pid = _postPid, let win = SkyLight.frontWindow(ofPid: pid) {
        if SkyLight.click(x: x, y: y, pid: pid, wid: win.wid, winOrigin: win.origin) { return }
    }
    let pt = CGPoint(x: x, y: y)
    let restore = _postPid == nil ? CGEvent(source: nil)?.location : nil
    let down = CGEvent(mouseEventSource: _src, mouseType: .leftMouseDown, mouseCursorPosition: pt, mouseButton: .left)
    let up = CGEvent(mouseEventSource: _src, mouseType: .leftMouseUp, mouseCursorPosition: pt, mouseButton: .left)
    postEvent(down)
    usleep(20000)
    postEvent(up)
    if let r = restore {
        usleep(10000)
        CGWarpMouseCursorPosition(r)
        CGAssociateMouseAndMouseCursorPosition(boolean_t(1))  // re-couple HID after the warp
    }
}

// MARK: - Multi-display geometry (AX top-left-origin global coords → Cocoa view-local)

/// Spans ALL screens so an overlay covers windows on secondary displays too (the actuated app may
/// be on a monitor above/beside the primary — its AX y can even be negative). Converts an AX global
/// point/rect (top-left origin of the PRIMARY screen, y down) into this union view's local coords.
struct ScreenSpace {
    let unionFrame: NSRect
    let primaryH: CGFloat
    init() {
        var u = NSScreen.screens.first?.frame ?? .zero
        for s in NSScreen.screens { u = u.union(s.frame) }
        unionFrame = u
        primaryH = NSScreen.screens.first?.frame.height ?? u.height
    }
    /// AX point → view-local (origin at unionFrame.origin).
    func point(_ x: Double, _ y: Double) -> NSPoint {
        NSPoint(x: x - Double(unionFrame.minX), y: Double(primaryH) - y - Double(unionFrame.minY))
    }
    /// AX rect [x,y,w,h] → view-local NSRect (bottom-left origin).
    func rect(_ f: [Double]) -> NSRect {
        NSRect(x: f[0] - Double(unionFrame.minX),
               y: Double(primaryH) - f[1] - f[3] - Double(unionFrame.minY),
               width: f[2], height: f[3])
    }
    /// AX point → GLOBAL Cocoa coords (bottom-left origin at the primary display's bottom-left).
    /// The reliable basis for PER-SCREEN overlay windows: a single union-spanning window does not
    /// composite across displays, so each screen's window subtracts its own origin from these.
    func gpoint(_ x: Double, _ y: Double) -> NSPoint {
        NSPoint(x: x, y: Double(primaryH) - y)
    }
    /// AX rect [x,y,w,h] → GLOBAL Cocoa NSRect (bottom-left origin).
    func grect(_ f: [Double]) -> NSRect {
        NSRect(x: f[0], y: Double(primaryH) - f[1] - f[3], width: f[2], height: f[3])
    }
}

// MARK: - Debug overlay (Set-of-Marks: frame every element's bbox on screen)

/// A click-through view that strokes each element's bbox + draws its index, color-coded by role.
final class OverlayView: NSView {
    let boxes: [(rect: NSRect, idx: Int, color: NSColor)]
    init(frame: NSRect, boxes: [(rect: NSRect, idx: Int, color: NSColor)]) {
        self.boxes = boxes
        super.init(frame: frame)
    }
    required init?(coder: NSCoder) { nil }

    override func draw(_ dirtyRect: NSRect) {
        for b in boxes {
            b.color.withAlphaComponent(0.9).setStroke()
            let p = NSBezierPath(rect: b.rect)
            p.lineWidth = 1.5
            p.stroke()
            // index tag, top-left of the box
            let tag = " \(b.idx) " as NSString
            let attrs: [NSAttributedString.Key: Any] = [
                .font: NSFont.boldSystemFont(ofSize: 9),
                .foregroundColor: NSColor.white,
                .backgroundColor: b.color.withAlphaComponent(0.85),
            ]
            tag.draw(at: NSPoint(x: b.rect.minX, y: b.rect.maxY - 12), withAttributes: attrs)
        }
    }
}

func colorForRole(_ role: String) -> NSColor {
    switch role {
    case "AXButton", "AXMenuItem", "AXMenuBarItem": return .systemGreen
    case "AXTextField", "AXTextArea", "AXComboBox": return .systemBlue
    case "AXCheckBox", "AXRadioButton", "AXPopUpButton": return .systemOrange
    case "AXLink": return .systemPurple
    default: return .systemGray
    }
}

/// Draw a debug overlay over ALL displays for `seconds`, framing every snapshot element (handles the
/// app being on a secondary monitor, incl. negative AX coords — see `ScreenSpace`).
func runOverlay(elements: [[String: Any]], seconds: Double) {
    let space = ScreenSpace()
    var boxes: [(rect: NSRect, idx: Int, color: NSColor)] = []
    for (i, e) in elements.enumerated() {
        guard let f = e["bbox"] as? [Double], f.count == 4, f[2] > 0, f[3] > 0 else { continue }
        boxes.append((space.rect(f), i, colorForRole(e["role"] as? String ?? "")))
    }

    let app = NSApplication.shared
    app.setActivationPolicy(.accessory)
    let win = NSWindow(contentRect: space.unionFrame, styleMask: .borderless, backing: .buffered, defer: false)
    win.isOpaque = false
    win.backgroundColor = .clear
    win.level = .screenSaver           // above normal app windows
    win.ignoresMouseEvents = true      // click-through: doesn't steal interaction
    win.collectionBehavior = [.canJoinAllSpaces, .stationary, .ignoresCycle]
    win.contentView = OverlayView(frame: NSRect(origin: .zero, size: space.unionFrame.size), boxes: boxes)
    win.setFrame(space.unionFrame, display: true)
    win.orderFrontRegardless()

    Timer.scheduledTimer(withTimeInterval: seconds, repeats: false) { _ in app.terminate(nil) }
    FileHandle.standardError.write(Data("overlay: \(boxes.count) boxes for \(seconds)s\n".utf8))
    app.run()
}

// MARK: - Action feedback (Persome cursor + optional bbox boxes during an act)

/// A click-through layer drawn while Persome performs an action: a distinctive **Persome cursor** ring at
/// the action point (so the user sees Persome is operating the app — like Claude Code's computer-use
/// cursor), optionally over the app's element boxes (the "what Persome can touch" debug frames).
final class FeedbackView: NSView {
    var point: NSPoint?          // already flipped to this view's (bottom-left) coords
    var boxes: [(rect: NSRect, idx: Int, color: NSColor)]
    var note: String             // short "what Persome is doing this step" label, shown in a bubble
    // Takeover glow (spec 2026-07-02-takeover-glow-overlay): the driven window's frame in this
    // view's local coords + the state color; `glowBreath` (0…1) is the breathing phase and
    // `glowBloom` (0…1) the appear envelope — a fresh glow eases in over ~0.8s instead of popping
    // (it lands on the beat of the voice flow's screen-catch animation). Owned by
    // `GlowController`, updated on its animation timer — independent of the cursor fields above.
    var glowRect: NSRect?
    var glowColor: NSColor = .clear
    var glowBreath: CGFloat = 0
    var glowBloom: CGFloat = 1
    init(frame: NSRect, point: NSPoint?, note: String,
         boxes: [(rect: NSRect, idx: Int, color: NSColor)]) {
        self.point = point
        self.note = note
        self.boxes = boxes
        super.init(frame: frame)
    }
    required init?(coder: NSCoder) { nil }

    /// Live update (persistent cursor HUD): move the cursor, change the note/boxes, redraw.
    func update(point: NSPoint?, note: String, boxes: [(rect: NSRect, idx: Int, color: NSColor)]) {
        self.point = point; self.note = note; self.boxes = boxes
        needsDisplay = true
    }

    /// Live update (glow layer): invalidates only the glow's neighborhood — this view spans a whole
    /// screen and the breathing timer ticks ~15×/s, so a full redraw would repaint everything.
    func updateGlow(rect: NSRect?, color: NSColor, breath: CGFloat, bloom: CGFloat = 1) {
        let pad: CGFloat = 70   // widest halo blur + line width
        if let old = glowRect { setNeedsDisplay(old.insetBy(dx: -pad, dy: -pad)) }
        glowRect = rect; glowColor = color; glowBreath = breath; glowBloom = bloom
        if let new = rect { setNeedsDisplay(new.insetBy(dx: -pad, dy: -pad)) }
    }

    private let persome = NSColor(calibratedRed: 0.85, green: 0.33, blue: 0.18, alpha: 1)  // terracotta

    override func draw(_ dirtyRect: NSRect) {
        if let g = glowRect {
            // Breathing halo, Arco-tuned (spec §4.5): a THIN restrained stroke with a wide diffuse
            // same-color shadow riding the phase — the light should bloom, not box the window in.
            // Drawn twice so the outer glow reads through the shadow accumulation.
            let path = NSBezierPath(roundedRect: g, xRadius: 12, yRadius: 12)
            NSGraphicsContext.saveGraphicsState()
            let sh = NSShadow()
            sh.shadowColor = glowColor.withAlphaComponent((0.35 + 0.4 * glowBreath) * glowBloom)
            sh.shadowBlurRadius = 14 + 18 * glowBreath
            sh.set()
            glowColor.withAlphaComponent((0.55 + 0.3 * glowBreath) * glowBloom).setStroke()
            path.lineWidth = (1.5 + 1.2 * glowBreath) * (0.55 + 0.45 * glowBloom)
            path.stroke()
            path.stroke()
            NSGraphicsContext.restoreGraphicsState()
        }
        for b in boxes {
            b.color.withAlphaComponent(0.10).setFill()
            NSBezierPath(rect: b.rect).fill()          // faint tint so the element reads as "live"
            b.color.withAlphaComponent(0.9).setStroke()
            let p = NSBezierPath(rect: b.rect); p.lineWidth = 2.5; p.stroke()
            let tag = " \(b.idx) " as NSString          // index tag (top-left), matches the snapshot list
            let attrs: [NSAttributedString.Key: Any] = [
                .font: NSFont.boldSystemFont(ofSize: 10),
                .foregroundColor: NSColor.white,
                .backgroundColor: b.color.withAlphaComponent(0.85),
            ]
            tag.draw(at: NSPoint(x: b.rect.minX, y: b.rect.maxY - 13), withAttributes: attrs)
        }
        guard let pt = point else { return }
        // outer halo
        for (r, a) in [(26.0, 0.18), (18.0, 0.30)] {
            persome.withAlphaComponent(a).setFill()
            NSBezierPath(ovalIn: NSRect(x: pt.x - r, y: pt.y - r, width: r * 2, height: r * 2)).fill()
        }
        // ring
        persome.setStroke()
        let ring = NSBezierPath(ovalIn: NSRect(x: pt.x - 12, y: pt.y - 12, width: 24, height: 24))
        ring.lineWidth = 2.5; ring.stroke()
        // center spark mark
        ("✦" as NSString).draw(at: NSPoint(x: pt.x - 6, y: pt.y - 8),
                  withAttributes: [.font: NSFont.boldSystemFont(ofSize: 13), .foregroundColor: persome])
        drawBubble(at: pt)
    }

    /// A rounded speech bubble next to the cursor showing the step note (✦ persome · <note>).
    private func drawBubble(at pt: NSPoint) {
        let text = note.isEmpty ? "persome" : "✦ \(note)"
        let font = NSFont.boldSystemFont(ofSize: 12)
        let attrs: [NSAttributedString.Key: Any] = [.font: font, .foregroundColor: NSColor.white]
        let maxW: CGFloat = 360
        let textSize = (text as NSString).boundingRect(
            with: NSSize(width: maxW, height: 80),
            options: [.usesLineFragmentOrigin], attributes: attrs).size
        let padX: CGFloat = 12, padY: CGFloat = 7
        let bw = min(textSize.width, maxW) + padX * 2
        let bh = textSize.height + padY * 2
        // Place above-right of the cursor; nudge left if it would run off the right edge.
        var bx = pt.x + 22
        if bx + bw > bounds.width - 8 { bx = max(8, pt.x - bw - 22) }
        let by = min(pt.y + 14, bounds.height - bh - 8)
        let bubble = NSRect(x: bx, y: by, width: bw, height: bh)
        NSColor(calibratedWhite: 0.10, alpha: 0.92).setFill()
        let path = NSBezierPath(roundedRect: bubble, xRadius: 9, yRadius: 9)
        path.fill()
        persome.setStroke(); path.lineWidth = 1.5; path.stroke()
        (text as NSString).draw(
            in: NSRect(x: bx + padX, y: by + padY, width: bw - padX * 2, height: bh - padY * 2),
            withAttributes: attrs)
    }
}

// MARK: - Takeover glow + badge (spec 2026-07-02-takeover-glow-overlay-design.md)

/// The clickable status pill at the driven window's top-right corner: `✦ Persome · <state/note> | 💬 | ⏹`.
/// Lives in its own small NON-click-through panel (the glow panes stay fully click-through).
/// Three hot zones, right to left:
///   • ⏹ — stops the run (`mens-app://task/<id>/stop`); only present while stoppable AND attributable
///     (live state + a task id; codex runs carry none → no stop zone, like before).
///   • 💬 — opens the take-over panel for the driven APP (`mens-app://app/<bundle-id>/panel`); the
///     explicit "view this window's agent conversation + records" entry (epic P0-2). Present whenever
///     the driven app's bundle id is known. This is the ONLY zone that opens the floating panel.
///   • body — the DEFAULT action does NOT open the panel (spec D2): it just surfaces the run's detail
///     in Persome (`mens-app://task/<id>`, or `mens-app://` when there's no task id).
final class BadgeView: NSView {
    private var text = ""
    private var color = NSColor.white
    var taskId = ""
    /// Bundle id of the driven app — the 💬 panel zone's target. Empty → no panel zone.
    var bundleID = ""
    /// True while the run is live (observing/executing/awaiting_confirm) — terminal flashes
    /// (done/failed) drop the stop zone (nothing left to stop).
    var stoppable = false

    /// Width of the ⏹ hot zone incl. its hairline divider. 0 when hidden.
    private var stopZoneWidth: CGFloat { (stoppable && !taskId.isEmpty) ? 26 : 0 }
    /// Width of the 💬 panel hot zone incl. its hairline divider. 0 when the bundle id is unknown.
    private var chatZoneWidth: CGFloat { bundleID.isEmpty ? 0 : 24 }

    private var attrs: [NSAttributedString.Key: Any] {
        [.font: NSFont.boldSystemFont(ofSize: 11.5), .foregroundColor: NSColor.white]
    }

    func setText(_ t: String, color: NSColor, stoppable: Bool, bundleID: String) {
        guard t != text || color != self.color || stoppable != self.stoppable
            || bundleID != self.bundleID else { return }
        text = t; self.color = color; self.stoppable = stoppable; self.bundleID = bundleID
        needsDisplay = true
    }

    /// The bubble's pixel size for the current text (the panel is resized to fit).
    /// Width = spark glyph (~15px incl. gap) + text + side padding + the 💬 and ⏹ zones.
    func desiredSize() -> NSSize {
        let s = (text as NSString).size(withAttributes: attrs)
        return NSSize(width: min(s.width, 300) + 24 + 15 + chatZoneWidth + stopZoneWidth,
                      height: s.height + 12)
    }

    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

    override func mouseDown(with event: NSEvent) {
        // Zones right→left: ⏹ stop, else → OPEN THE PANEL (the whole badge is a panel entry now, not
        // just the 💬 zone). The badge must NEVER surface Persome's retiring main window (Home/Calendar,
        // D3) — so the body no longer deep-links to `task/<id>` (which routed `router.openTask` → the
        // main window); it opens the app's take-over panel by bundle id, same as 💬. Falls back to a
        // bare `mens-app://` only when we don't even know the driven app's bundle id (codex, no pid map).
        // taskId is sanitized at ingest (uuid charset); bundle ids are reverse-DNS (`[A-Za-z0-9.-]`,
        // one path component) — both safe to interpolate into the URL.
        let x = convert(event.locationInWindow, from: nil).x
        let inStopZone = stopZoneWidth > 0 && x > bounds.width - stopZoneWidth
        let target: String
        if inStopZone {
            target = "mens-app://task/\(taskId)/stop"
        } else if !bundleID.isEmpty {
            target = "mens-app://app/\(bundleID)/panel"
        } else {
            target = "mens-app://"
        }
        // Open WITHOUT activating Persome: a plain `NSWorkspace.open` brings Persome to the front and steals
        // focus from the taken-over app (LaunchServices activates the URL handler by default). The panel
        // is a non-activating NSPanel, so the app can show it while staying in the background — keep the
        // user's focus on the app they're driving. `activates = false` is exactly that.
        if let url = URL(string: target) {
            let cfg = NSWorkspace.OpenConfiguration()
            cfg.activates = false
            NSWorkspace.shared.open(url, configuration: cfg, completionHandler: nil)
        }
    }

    /// A vertical hairline zone divider at `x`, matching the ⏹/💬 zone treatment.
    private func drawZoneDivider(at x: CGFloat) {
        NSColor(calibratedWhite: 1, alpha: 0.18).setStroke()
        let divider = NSBezierPath()
        divider.move(to: NSPoint(x: x, y: 5))
        divider.line(to: NSPoint(x: x, y: bounds.height - 5))
        divider.lineWidth = 1
        divider.stroke()
    }

    override func draw(_ dirtyRect: NSRect) {
        // Feishu-style dark tooltip bubble (Arco gray-10 #1D2129, small 8px radius — spec §4.5),
        // 1px state-color hairline, the ✦ spark in the state color (it drifts with the glow).
        let bubble = NSBezierPath(roundedRect: bounds.insetBy(dx: 0.5, dy: 0.5),
                                  xRadius: 8, yRadius: 8)
        NSColor(calibratedRed: 0.114, green: 0.129, blue: 0.161, alpha: 0.92).setFill()
        bubble.fill()
        color.withAlphaComponent(0.9).setStroke()
        bubble.lineWidth = 1
        bubble.stroke()
        ("✦" as NSString).draw(
            at: NSPoint(x: 11, y: (bounds.height - 15) / 2),
            withAttributes: [.font: NSFont.boldSystemFont(ofSize: 12), .foregroundColor: color])
        (text as NSString).draw(
            in: NSRect(x: 26, y: 5.5,
                       width: bounds.width - 26 - 12 - chatZoneWidth - stopZoneWidth,
                       height: bounds.height - 10),
            withAttributes: attrs)
        // 💬 panel zone — a small speech-bubble outline, same monochrome treatment as ⏹.
        if chatZoneWidth > 0 {
            let zx = bounds.width - stopZoneWidth - chatZoneWidth
            drawZoneDivider(at: zx)
            let cx = zx + chatZoneWidth / 2
            let bw: CGFloat = 12, bh: CGFloat = 9
            let rect = NSRect(x: cx - bw / 2, y: (bounds.height - bh) / 2 + 1, width: bw, height: bh)
            let path = NSBezierPath(roundedRect: rect, xRadius: 2.5, yRadius: 2.5)
            // A little tail at the bottom-left, so it reads as a chat bubble not a plain box.
            let tail = NSBezierPath()
            tail.move(to: NSPoint(x: rect.minX + 2.5, y: rect.minY))
            tail.line(to: NSPoint(x: rect.minX + 1, y: rect.minY - 2.5))
            tail.line(to: NSPoint(x: rect.minX + 5, y: rect.minY))
            NSColor(calibratedWhite: 1, alpha: 0.85).setStroke()
            path.lineWidth = 1.2
            path.stroke()
            NSColor(calibratedWhite: 1, alpha: 0.85).setFill()
            tail.fill()
        }
        // ⏹ stop zone — hairline divider + the stop square, legible but not competing with the label.
        if stopZoneWidth > 0 {
            let zx = bounds.width - stopZoneWidth
            drawZoneDivider(at: zx)
            let s: CGFloat = 8
            let square = NSRect(x: zx + (stopZoneWidth - s) / 2 - 1,
                                y: (bounds.height - s) / 2, width: s, height: s)
            NSColor(calibratedWhite: 1, alpha: 0.85).setFill()
            NSBezierPath(roundedRect: square, xRadius: 1.5, yRadius: 1.5).fill()
        }
    }
}

/// Renders the takeover glow + badge for ONE driven window, fed by `{"glow": …}` stdin lines.
/// Re-reads the target window's AX frame on the breathing timer (≈5 Hz frame poll under a ≈15 fps
/// animation) so the halo follows drags/resizes live, and hides both layers whenever the window
/// isn't actually visible — minimized, app hidden, another Space, or an off-screen virtual display
/// (visibility = an on-screen CGWindowList row of the same pid with a near-identical frame).
/// Main-thread only.
final class GlowController {
    private let panes: [(view: FeedbackView, origin: NSPoint)]
    private let space: ScreenSpace
    private var timer: Timer?

    private var pid: pid_t = 0
    private var state = ""
    private var note = ""
    private var taskId = ""
    private var point: (x: Double, y: Double)?   // last act's AX point — pins the hit window
    private var windowEl: AXUIElement?
    private var lastFrame: [Double]?
    private var visible = false
    private var terminalDeadline: Date?          // done/failed hold ~2.5s, then self-clear
    private var phase = 0.0
    private var tick = 0
    /// Appear envelope: a FRESH glow eases in over ~0.8s (cubic ease-out) instead of popping —
    /// timed by the app to land right as the voice flow's screen-catch animation finishes, so the
    /// halo reads as that gesture's continuation. nil once fully bloomed (steady state).
    private var appearedAt: Date?
    private static let appearSeconds = 0.8

    private var badgePanel: NSPanel?
    private var badgeView: BadgeView?

    init(panes: [(view: FeedbackView, origin: NSPoint)], space: ScreenSpace) {
        self.panes = panes
        self.space = space
    }

    /// Apply one `{"glow": …}` message ({app, pid, state, note, task_id, point?} or {clear:true}).
    func apply(_ obj: [String: Any]) {
        if obj["clear"] as? Bool == true { teardown(); return }
        guard let st = obj["state"] as? String, !st.isEmpty else { return }
        if let p = obj["pid"] as? Int, p > 0 { pid = pid_t(p) }
        if pid <= 0, let name = obj["app"] as? String, let p = pidForApp(name) { pid = p }
        guard pid > 0 else { return }
        state = st
        note = obj["note"] as? String ?? ""
        // uuid charset only — the id is interpolated into a URL.
        taskId = ((obj["task_id"] as? String) ?? "").filter { $0.isLetter || $0.isNumber || $0 == "-" }
        if let pt = obj["point"] as? [Double], pt.count == 2 { point = (pt[0], pt[1]) }
        terminalDeadline = (st == "done" || st == "failed") ? Date().addingTimeInterval(2.5) : nil
        refreshFrame()
        if timer == nil {
            // Fresh activation (no live glow): bloom in from the BLUE end of the drift
            // (sin(-3.49 × 0.45) ≈ −1 → pure arcoblue) so the halo always rises blue.
            appearedAt = Date()
            phase = -3.49
            let t = Timer(timeInterval: 1.0 / 15.0, repeats: true) { [weak self] _ in self?.onTick() }
            RunLoop.main.add(t, forMode: .common)
            timer = t
        }
        render()
    }

    private func onTick() {
        if let dl = terminalDeadline, Date() > dl { teardown(); return }
        // awaiting_confirm pulses at ~3× the executing breath, pulling the eye to the approval.
        phase += (state == "awaiting_confirm") ? 0.55 : 0.18
        tick += 1
        if tick % 3 == 1 { refreshFrame() }   // AX reads at ~5 Hz; drawing stays smooth at 15
        render()
    }

    /// Re-resolve the target window + its frame + whether it is actually visible to the user.
    private func refreshFrame() {
        guard pid > 0 else { visible = false; return }
        guard let app = NSRunningApplication(processIdentifier: pid), !app.isTerminated else {
            teardown(); return
        }
        if app.isHidden { visible = false; return }
        resolveWindowIfNeeded()
        guard let w = windowEl, let f = axFrame(w), f[2] > 4, f[3] > 4 else {
            windowEl = nil          // window closed/stale — re-resolve next tick
            visible = false
            return
        }
        if axBool(w, "AXMinimized") { visible = false; return }
        guard windowIsOnScreen(pid: pid, axFrame: f) else { visible = false; return }
        lastFrame = f
        visible = true
    }

    /// Pick WHICH window glows (product decision: the one the actuation actually hit): the app
    /// window containing the last act point; else the focused window; else the first one. Once
    /// resolved, the same AXUIElement is kept until it dies or a new act point lands elsewhere.
    private func resolveWindowIfNeeded() {
        let appEl = AXUIElementCreateApplication(pid)
        var ref: CFTypeRef?
        let wins: [AXUIElement]
        if AXUIElementCopyAttributeValue(appEl, kAXWindowsAttribute as CFString, &ref) == .success,
           let list = ref as? [AXUIElement] {
            wins = list
        } else { wins = [] }
        if let pt = point {
            // The hit window wins — including switching an already-resolved glow when a later act
            // lands in a different window of the same app.
            for w in wins {
                if let f = axFrame(w), f[2] > 4, f[3] > 4,
                   pt.x >= f[0], pt.x <= f[0] + f[2], pt.y >= f[1], pt.y <= f[1] + f[3] {
                    windowEl = w
                    return
                }
            }
        }
        if windowEl != nil, axFrame(windowEl!) != nil { return }   // current one still alive
        var fref: CFTypeRef?
        if AXUIElementCopyAttributeValue(appEl, kAXFocusedWindowAttribute as CFString, &fref) == .success,
           let f = fref {
            // swiftlint:disable:next force_cast
            windowEl = (f as! AXUIElement)
            return
        }
        windowEl = wins.first
    }

    private func axBool(_ el: AXUIElement, _ attr: String) -> Bool {
        var ref: CFTypeRef?
        guard AXUIElementCopyAttributeValue(el, attr as CFString, &ref) == .success else { return false }
        return (ref as? Bool) ?? false
    }

    /// True when an on-screen window of `pid` matches this AX frame — false for a window sitting on
    /// another Space / a fullscreen Space we're not on / an off-screen virtual display. CGWindow
    /// bounds share AX's top-left-origin global coords, so they compare directly. Bounds+pid need
    /// no extra TCC (only window *names* require Screen Recording). Fail-open: no list → visible.
    private func windowIsOnScreen(pid: pid_t, axFrame f: [Double]) -> Bool {
        guard let list = CGWindowListCopyWindowInfo(.optionOnScreenOnly, kCGNullWindowID)
                as? [[String: Any]] else { return true }
        for w in list {
            guard let owner = w[kCGWindowOwnerPID as String] as? Int32, owner == Int32(pid),
                  let b = w[kCGWindowBounds as String] as? [String: Any],
                  let bx = b["X"] as? Double, let by = b["Y"] as? Double,
                  let bw = b["Width"] as? Double, let bh = b["Height"] as? Double else { continue }
            if abs(bx - f[0]) < 6, abs(by - f[1]) < 6, abs(bw - f[2]) < 12, abs(bh - f[3]) < 12 {
                return true
            }
        }
        return false
    }

    // Arco Design palette (Feishu's design language, spec §4.5 — all -6 primary tokens).
    // "务实的浪漫主义": the restrained base is neutral/functional; the romance lives only in the
    // executing state's slow arcoblue↔purple drift (the "AI is working" layer, à la Feishu AI).
    private static let arcoBlue = NSColor(calibratedRed: 0.086, green: 0.365, blue: 1.0, alpha: 1)      // #165DFF
    private static let arcoPurple = NSColor(calibratedRed: 0.447, green: 0.180, blue: 0.820, alpha: 1)  // #722ED1
    private static let arcoGray = NSColor(calibratedRed: 0.525, green: 0.565, blue: 0.612, alpha: 1)    // #86909C
    private static let arcoOrange = NSColor(calibratedRed: 1.0, green: 0.490, blue: 0.0, alpha: 1)      // #FF7D00
    private static let arcoGreen = NSColor(calibratedRed: 0.0, green: 0.706, blue: 0.165, alpha: 1)     // #00B42A
    private static let arcoRed = NSColor(calibratedRed: 0.961, green: 0.247, blue: 0.247, alpha: 1)     // #F53F3F

    /// Base color + optional drift partner. Only `executing` gets the two-tone drift.
    private func stateColors() -> (base: NSColor, drift: NSColor?) {
        switch state {
        case "observing": return (Self.arcoGray, nil)
        case "awaiting_confirm": return (Self.arcoOrange, nil)
        case "done": return (Self.arcoGreen, nil)
        case "failed": return (Self.arcoRed, nil)
        default: return (Self.arcoBlue, Self.arcoPurple)  // executing
        }
    }

    /// The color for THIS animation frame: single tone for the restrained states; for executing,
    /// a slow (~5s period, slower than the ~2.3s breath) interpolation between arcoblue and purple.
    private func stateColor() -> NSColor {
        let (base, drift) = stateColors()
        guard let drift else { return base }
        let t = CGFloat((sin(phase * 0.45) + 1) / 2) * 0.85
        return base.blended(withFraction: t, of: drift) ?? base
    }

    /// Badge label — the state axis supplies the fallback wording, the note axis the specifics.
    /// (The ✦ spark is drawn separately by BadgeView, in the state color.)
    private func badgeText() -> String {
        let n = note.count > 30 ? String(note.prefix(30)) + "…" : note
        let label: String
        switch state {
        case "observing": label = n.isEmpty ? "正在观察" : n
        case "awaiting_confirm": label = "等待确认：" + (n.isEmpty ? "…" : n)
        case "done": label = "已完成"
        case "failed": label = "已停止"
        default: label = n.isEmpty ? "正在操作" : n
        }
        return "Persome · \(label)"
    }

    /// The appear envelope value for THIS frame: cubic ease-out 0→1 across `appearSeconds`,
    /// steady 1 afterwards. "务实" — one gentle rise, no bounce, no overshoot.
    private func bloomFactor() -> CGFloat {
        guard let a = appearedAt else { return 1 }
        let x = Date().timeIntervalSince(a) / Self.appearSeconds
        if x >= 1 { appearedAt = nil; return 1 }
        let inv = 1 - x
        return CGFloat(1 - inv * inv * inv)
    }

    private func render() {
        let breath = CGFloat((sin(phase) + 1) / 2)
        let bloom = bloomFactor()
        guard visible, let f = lastFrame, terminalOrLiveStateIsRenderable() else {
            for (view, _) in panes { view.updateGlow(rect: nil, color: .clear, breath: 0) }
            badgePanel?.orderOut(nil)
            return
        }
        let grect = space.grect(f)
        for (view, origin) in panes {
            let local = grect.offsetBy(dx: -origin.x, dy: -origin.y)
            let show = local.intersects(view.bounds)
            view.updateGlow(rect: show ? local : nil, color: stateColor(), breath: breath, bloom: bloom)
        }
        renderBadge(around: grect, bloom: bloom)
    }

    /// A tiny guard so a malformed state string never renders a stray glow.
    private func terminalOrLiveStateIsRenderable() -> Bool { !state.isEmpty }

    private func renderBadge(around winRect: NSRect, bloom: CGFloat = 1) {
        let (panel, view) = ensureBadge()
        view.taskId = taskId
        // The ⏹ zone exists only while the run is live — a terminal flash has nothing to stop.
        let live = state == "observing" || state == "executing" || state == "awaiting_confirm"
        // The 💬 panel zone targets the driven APP by bundle id (resolved from its pid).
        view.setText(badgeText(), color: stateColor(), stoppable: live, bundleID: bundleForPid(pid))
        let size = view.desiredSize()
        // Inside the window's top-right corner (badge top inset 6, right inset 10).
        let x = winRect.maxX - size.width - 10
        let y = winRect.maxY - size.height - 6
        panel.setFrame(NSRect(x: max(x, winRect.minX + 4), y: max(y, winRect.minY + 4),
                              width: size.width, height: size.height), display: true)
        panel.alphaValue = bloom   // the badge floats in with the halo's appear envelope
        panel.orderFrontRegardless()
    }

    private func ensureBadge() -> (NSPanel, BadgeView) {
        if let p = badgePanel, let v = badgeView { return (p, v) }
        let panel = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 10, height: 10),
                            styleMask: [.borderless, .nonactivatingPanel],
                            backing: .buffered, defer: false)
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.level = .screenSaver
        panel.hidesOnDeactivate = false
        panel.collectionBehavior = [.canJoinAllSpaces, .stationary, .ignoresCycle,
                                    .fullScreenAuxiliary]
        let view = BadgeView(frame: NSRect(x: 0, y: 0, width: 10, height: 10))
        view.autoresizingMask = [.width, .height]
        panel.contentView = view
        badgePanel = panel
        badgeView = view
        return (panel, view)
    }

    private func teardown() {
        timer?.invalidate()
        timer = nil
        for (view, _) in panes { view.updateGlow(rect: nil, color: .clear, breath: 0) }
        badgePanel?.orderOut(nil)
        pid = 0
        state = ""
        note = ""
        taskId = ""
        point = nil
        windowEl = nil
        lastFrame = nil
        visible = false
        terminalDeadline = nil
        phase = 0
        tick = 0
        appearedAt = nil
    }
}

/// Flash the Persome cursor (and optional element boxes) at an action point for `seconds`. `axPoint`
/// is in AX top-left-origin coords; it's flipped against the main screen. No-op if there's nothing
/// to show (no point and no boxes).
func showActionFeedback(axPoint: (x: Double, y: Double)?, note: String, elements: [[String: Any]],
                        showBoxes: Bool, seconds: Double) {
    let space = ScreenSpace()
    var boxes: [(rect: NSRect, idx: Int, color: NSColor)] = []
    if showBoxes {
        for (i, e) in elements.enumerated() {
            guard let f = e["bbox"] as? [Double], f.count == 4, f[2] > 0, f[3] > 0 else { continue }
            boxes.append((space.rect(f), i, colorForRole(e["role"] as? String ?? "")))
        }
    }
    let pt = axPoint.map { space.point($0.x, $0.y) }
    if pt == nil && boxes.isEmpty { return }

    let app = NSApplication.shared
    app.setActivationPolicy(.accessory)
    let win = NSWindow(contentRect: space.unionFrame, styleMask: .borderless, backing: .buffered, defer: false)
    win.isOpaque = false; win.backgroundColor = .clear
    win.level = .screenSaver; win.ignoresMouseEvents = true
    win.collectionBehavior = [.canJoinAllSpaces, .stationary, .ignoresCycle]
    win.contentView = FeedbackView(frame: NSRect(origin: .zero, size: space.unionFrame.size),
                                   point: pt, note: note, boxes: boxes)
    win.setFrame(space.unionFrame, display: true)
    win.orderFrontRegardless()
    Timer.scheduledTimer(withTimeInterval: seconds, repeats: false) { _ in app.terminate(nil) }
    app.run()
}

/// AX-coord center of an element's frame, or nil.
func centerOf(_ el: AXUIElement) -> (x: Double, y: Double)? {
    guard let f = axFrame(el), f[2] > 0 || f[3] > 0 else { return nil }
    return (f[0] + f[2] / 2, f[1] + f[3] / 2)
}

/// AX-coord center of the app's focused UI element (for key/type feedback).
func focusedCenter(_ appEl: AXUIElement) -> (x: Double, y: Double)? {
    var ref: CFTypeRef?
    guard AXUIElementCopyAttributeValue(appEl, kAXFocusedUIElementAttribute as CFString, &ref) == .success,
          let el = ref else { return nil }
    // swiftlint:disable:next force_cast
    return centerOf(el as! AXUIElement)
}

// MARK: - JSON out

func emit(_ obj: [String: Any]) {
    let data = (try? JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys])) ?? Data("{}".utf8)
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data("\n".utf8))
}

/// Phase tracker + hard deadline for the `act` subcommand (issue #466). The CGEvent freeform verbs
/// (key/type/clickxy) intermittently wedged >10s producing NOTHING — the daemon killed them at its
/// subprocess timeout and every trace of WHERE they wedged died with them (unreproducible later).
/// This makes a recurrence (a) fast — the whole act hard-deadlines INSIDE the daemon's window, and
/// (b) diagnosable — each phase is stamped to stderr (the daemon logs the tail on failure) and the
/// deadline emission names the wedged phase, so the next occurrence pins the culprit call.
/// `claim()` makes emission single-shot: whichever of the main path / deadline thread gets there
/// first speaks; the loser stays silent (no interleaved JSON on stdout).
final class ActPhase {
    private let lock = NSLock()
    private var name = "start"
    private var claimed = false
    private let t0 = Date()

    private func elapsedMs() -> Int { Int(Date().timeIntervalSince(t0) * 1000) }

    /// Enter a phase: record it + stamp stderr (`[act] phase=<n> t=<ms>ms`).
    func set(_ n: String) {
        lock.lock(); name = n; lock.unlock()
        FileHandle.standardError.write(Data("[act] phase=\(n) t=\(elapsedMs())ms\n".utf8))
    }

    /// One-shot claim of the output channel. True for exactly one caller.
    func claim() -> Bool {
        lock.lock(); defer { lock.unlock() }
        if claimed { return false }
        claimed = true
        return true
    }

    /// Arm the whole-act deadline (default 8s — inside the daemon's 10s subprocess timeout;
    /// override for tests/tuning via PERSOME_ACT_DEADLINE_MS). If the act hasn't claimed the output
    /// by then, emit a structured, agent-recoverable error naming the wedged phase and exit.
    func armDeadline() {
        let ms = Int(ProcessInfo.processInfo.environment["PERSOME_ACT_DEADLINE_MS"] ?? "") ?? 8000
        DispatchQueue.global().asyncAfter(deadline: .now() + .milliseconds(ms)) { [self] in
            lock.lock(); let n = name; lock.unlock()
            guard claim() else { return }   // act finished normally — stand down
            FileHandle.standardError.write(Data("[act] DEADLINE phase=\(n) t=\(elapsedMs())ms\n".utf8))
            emit(["ok": false, "error": "act_deadline_exceeded", "phase": n,
                  "hint": "the '\(n)' step wedged (intermittent OS state, issue #466). The input " +
                          "event may or may not have landed — ui_snapshot the app to check its " +
                          "ACTUAL state before deciding whether to retry (key/type are not idempotent)."])
            exit(3)
        }
    }
}

// MARK: - arg parsing + main

func pidForApp(_ name: String) -> pid_t? {
    // Match by localized name OR bundle id — exact, suffix (`com.apple.calculator` ← "Calculator"),
    // or substring — so an agent can name an app without knowing the exact localized title (which is
    // locale-dependent) or the full bundle id.
    //
    // Chromium/Electron browsers register MANY same-named processes ("Tabbit", "Tabbit Helper
    // (Renderer)", …). A first-match loop with a loose `name.contains` happily returns a renderer
    // HELPER — whose AX tree is empty (raw_elements=0) — instead of the real browser. So score every
    // candidate and pick the best: exact bundle-id/name beats a suffix beats a substring, and a
    // foreground app (`.regular`) always beats an `.accessory`/`.prohibited` helper at the same tier.
    let q = name.lowercased()
    if q.isEmpty { return nil }
    var best: (score: Int, pid: pid_t)? = nil
    for app in NSWorkspace.shared.runningApplications {
        let ln = (app.localizedName ?? "").lowercased()
        let bid = (app.bundleIdentifier ?? "").lowercased()
        var score = 0
        if bid == q || ln == q { score = 4 }
        else if bid.hasSuffix("." + q) { score = 3 }
        else if ln.contains(q) || bid.contains(q) { score = 1 }
        else { continue }
        if app.activationPolicy == .regular { score += 4 } // a real app, not a background helper
        if best == nil || score > best!.score { best = (score, app.processIdentifier) }
    }
    return best?.pid
}

func bundleForPid(_ pid: pid_t) -> String {
    NSRunningApplication(processIdentifier: pid)?.bundleIdentifier ?? ""
}

func arg(_ flag: String, _ args: [String]) -> String? {
    guard let i = args.firstIndex(of: flag), i + 1 < args.count else { return nil }
    return args[i + 1]
}

let args = Array(CommandLine.arguments.dropFirst())
let sub = args.first ?? ""
let maxDepth = Int(arg("--depth", args) ?? "") ?? 60

func resolvePid(_ args: [String]) -> pid_t? {
    if let p = arg("--pid", args), let n = Int32(p) { return n }
    if let a = arg("--app", args) { return pidForApp(a) }
    return nil
}

switch sub {
case "trust":
    let trusted = AXIsProcessTrusted()
    emit(["trusted": trusted])
    exit(trusted ? 0 : 1)

case "probe-skylight":
    // Report which SkyLight private symbols resolve on this OS — the background-driver feasibility
    // gate. `available` = the no-steal click recipe can run; false → callers degrade.
    let p = SkyLight.probe()
    emit(["available": SkyLight.available, "symbols": p])
    exit(SkyLight.available ? 0 : 1)

case "snapshot":
    guard let pid = resolvePid(args) else { emit(["ok": false, "error": "no_pid_or_app"]); exit(2) }
    let _ts0 = Date().timeIntervalSince1970 * 1000
    let w = snapshotApp(pid: pid, maxDepth: maxDepth)
    let _ts1 = Date().timeIntervalSince1970 * 1000
    saveCachedState(pid, w.state)  // a following act can reuse this as its before-state (--cache-before)
    emit(["ok": true, "pid": Int(pid), "bundle_id": bundleForPid(pid), "elements": w.elements,
          "timing": ["snapshot_ms": _ts1 - _ts0]])

case "overlay":
    // Debug: snapshot the app and frame every element's bbox on screen (Set-of-Marks style),
    // color-coded by role, for `--seconds` (default 4). Held above all windows, click-through.
    guard let pid = resolvePid(args) else { emit(["ok": false, "error": "no_pid_or_app"]); exit(2) }
    let secs = Double(arg("--seconds", args) ?? "") ?? 4.0
    let w = snapshotApp(pid: pid, maxDepth: maxDepth)
    runOverlay(elements: w.elements, seconds: secs)

case "cursor-hud":
    // Persistent floating Persome cursor (daemon-driven): a long-lived click-through overlay. Reads
    // newline-JSON from stdin: {"x":<ax>,"y":<ax>,"note":"...","elements":[{bbox,role}]} moves the
    // cursor + bubble (+ optional boxes); {"hide":true} clears; EOF terminates. The daemon updates it
    // per act so the user sees one cursor float across the steps, instead of a per-act flash.
    //
    // ONE window PER SCREEN: a single union-spanning .screenSaver window does NOT composite across
    // displays (it only paints on one), so an element on a different display than that one renders
    // invisibly. Each screen gets its own window; coords are computed GLOBALLY (`grect`/`gpoint`)
    // then offset into each screen's local space, so every box/cursor lands on its real display.
    let space = ScreenSpace()
    let app = NSApplication.shared
    app.setActivationPolicy(.accessory)
    var panes: [(view: FeedbackView, origin: NSPoint)] = []
    for screen in NSScreen.screens {
        let win = NSWindow(contentRect: screen.frame, styleMask: .borderless, backing: .buffered, defer: false)
        win.isOpaque = false; win.backgroundColor = .clear
        win.level = .screenSaver; win.ignoresMouseEvents = true
        win.collectionBehavior = [.canJoinAllSpaces, .stationary, .ignoresCycle]
        let view = FeedbackView(frame: NSRect(origin: .zero, size: screen.frame.size),
                                point: nil, note: "", boxes: [])
        win.contentView = view
        win.setFrame(screen.frame, display: true)
        win.orderFrontRegardless()
        panes.append((view, screen.frame.origin))
    }
    FileHandle.standardError.write(Data("cursor-hud up (\(panes.count) screens)\n".utf8))
    let glow = GlowController(panes: panes, space: space)
    DispatchQueue.global(qos: .userInitiated).async {
        while let line = readLine(strippingNewline: true) {
            guard let data = line.data(using: .utf8),
                  let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { continue }
            // Takeover glow messages are standalone — routing them through the cursor path below
            // would wipe the live cursor/boxes (its update() replaces all three fields).
            if let g = obj["glow"] as? [String: Any] {
                DispatchQueue.main.async { glow.apply(g) }
                continue
            }
            let hide = obj["hide"] as? Bool == true
            let note = hide ? "" : (obj["note"] as? String ?? "")
            var gpt: NSPoint?
            if !hide, let x = obj["x"] as? Double, let y = obj["y"] as? Double { gpt = space.gpoint(x, y) }
            var gboxes: [(rect: NSRect, idx: Int, color: NSColor)] = []
            if !hide, let els = obj["elements"] as? [[String: Any]] {
                for (i, e) in els.enumerated() {
                    guard let f = e["bbox"] as? [Double], f.count == 4, f[2] > 0, f[3] > 0 else { continue }
                    gboxes.append((space.grect(f), i, colorForRole(e["role"] as? String ?? "")))
                }
            }
            DispatchQueue.main.async {
                for (view, origin) in panes {
                    // the cursor + bubble only on the screen that contains the point; boxes on every
                    // screen (each view clips to its own bounds, so a box shows on its real display).
                    let local = gpt.flatMap { p -> NSPoint? in
                        let lp = NSPoint(x: p.x - origin.x, y: p.y - origin.y)
                        return view.bounds.contains(lp) ? lp : nil
                    }
                    let boxes = gboxes.map {
                        (rect: $0.rect.offsetBy(dx: -origin.x, dy: -origin.y), idx: $0.idx, color: $0.color)
                    }
                    view.update(point: local, note: local == nil ? "" : note, boxes: boxes)
                }
            }
        }
        DispatchQueue.main.async { app.terminate(nil) }   // stdin closed → exit
    }
    app.run()

case "act":
    guard let pid = resolvePid(args) else { emit(["ok": false, "error": "no_pid_or_app"]); exit(2) }
    let verb = arg("--verb", args) ?? "press"
    // --bg / --bg-mode: deliver CGEvent verbs (key/type/clickxy) to this app's pid only, not the global
    // HID tap, so they don't move the real cursor or steal focus. (press/setvalue are AX — already
    // no-steal.) `--bg-mode skylight` additionally uses the SkyLight focus-without-raise + per-pid click
    // recipe so the click lands in a BACKGROUND Electron/Chromium target; bare `--bg` = legacy postToPid.
    let bgMode = arg("--bg-mode", args)
    if bgMode == "skylight" { _postPid = pid; _bgMode = .skylight }
    else if bgMode == "postpid" || args.contains("--bg") { _postPid = pid; _bgMode = .postpid }
    let appEl = AXUIElementCreateApplication(pid)
    AXUIElementSetAttributeValue(appEl, "AXManualAccessibility" as CFString, kCFBooleanTrue)

    // Phase watchdog (#466): stamp phases + hard-deadline the whole act inside the daemon's
    // subprocess timeout, so an intermittent wedge fails fast with the culprit phase named
    // instead of hanging silently until SIGKILL.
    let actPhase = ActPhase()
    actPhase.armDeadline()

    func nowMs() -> Double { Date().timeIntervalSince1970 * 1000 }
    // Before-state for the diff feedback. With --cache-before, reuse the previous act/snapshot's
    // after-state (the current state) instead of snapshotting again — halves this act's snapshot work.
    let _t0 = nowMs()
    actPhase.set("before_snapshot")
    let beforeState: [String: [String: String]] =
        (args.contains("--cache-before") ? loadCachedState(pid) : nil)
        ?? snapshotApp(pid: pid, maxDepth: maxDepth).state
    let _tSnapBefore = nowMs()
    actPhase.set("perform:\(verb)")

    var err: String?
    var actionPoint: (x: Double, y: Double)?   // AX-coord point to flash the Persome cursor at
    switch verb {
    case "key":   // CGEvent key combo, e.g. --keys "enter" / "cmd+k"
        err = postKeyCombo(arg("--keys", args) ?? "")
        actionPoint = focusedCenter(appEl)
    case "type":  // CGEvent Unicode typing into the focused field (incl. Chinese)
        if let t = arg("--text", args) { typeUnicode(t) } else { err = "missing_text" }
        actionPoint = focusedCenter(appEl)
    case "clickxy":  // CGEvent mouse click at a screen coord (focus a field AX-click won't take)
        if let x = Double(arg("--x", args) ?? ""), let y = Double(arg("--y", args) ?? "") {
            clickAt(x: x, y: y); actionPoint = (x, y)
        } else { err = "missing_xy" }
    default:      // element-targeted verbs: press / setvalue / confirm / action (need --id; action name in --text)
        guard let id = arg("--id", args) else { emit(["ok": false, "error": "missing_id"]); exit(2) }
        let (el, rerr) = resolve(appEl, id)
        if let rerr = rerr { emit(["ok": false, "error": rerr]); exit(0) }
        actionPoint = centerOf(el!)
        err = performVerb(el!, verb: verb, text: arg("--text", args))
    }

    let _tPerform = nowMs()
    // Let the UI settle, then capture after-state + diff (the action feedback). We keep the WHOLE
    // after-walk (not just .state) so the same snapshot also yields the current actionable elements —
    // the caller can hand the model "what's on screen now" without a SEPARATE ax_snapshot round-trip.
    actPhase.set("after_snapshot")
    usleep(120_000)
    let afterW = snapshotApp(pid: pid, maxDepth: maxDepth)
    let after = afterW.state
    saveCachedState(pid, after)  // the next act can reuse this as its before-state
    let diff = diffState(beforeState, after)
    let _tSnapAfter = nowMs()
    // `point` (AX coords of where the action happened) lets the daemon drive the persistent cursor HUD.
    let pointOut: Any = actionPoint.map { [$0.x, $0.y] } as Any
    // Internal phase timing (ms) for the flame graph: the before-snapshot, the perform (incl. the
    // 120ms settle), and the after-snapshot. The visual cursor/boxes flash happens AFTER this emit.
    let timing: [String: Double] = [
        "snap_before_ms": _tSnapBefore - _t0,
        "perform_ms": _tPerform - _tSnapBefore,
        "snap_after_ms": _tSnapAfter - _tPerform,
    ]
    // Single-shot output claim (#466): if the deadline thread already spoke (and is about to
    // exit(3)), stay silent — never interleave two JSON objects on stdout.
    guard actPhase.claim() else { exit(3) }
    emit(["ok": err == nil, "error": err as Any, "verb": verb, "diff": diff,
          "elements": afterW.elements, "point": pointOut, "timing": timing])

    // Visual feedback: flash the Persome cursor at the action point (default ON; --no-cursor disables)
    // and, optionally, the app's element boxes (--show-boxes) so the user sees Persome operating the app.
    let showCursor = !args.contains("--no-cursor")
    let showBoxes = args.contains("--show-boxes")
    if showCursor || showBoxes {
        let secs = Double(arg("--feedback-seconds", args) ?? "") ?? 0.6
        let els = showBoxes ? snapshotApp(pid: pid, maxDepth: maxDepth).elements : []
        showActionFeedback(axPoint: showCursor ? actionPoint : nil, note: arg("--note", args) ?? "",
                           elements: els, showBoxes: showBoxes, seconds: secs)
    }

default:
    FileHandle.standardError.write(Data("""
    Usage: mac-ax-actuator <snapshot|act|overlay|trust> [--pid N | --app NAME] [options]
      snapshot                       Emit addressable elements (id/role/label/value/bbox/actions) as JSON
      overlay  [--seconds N]         Debug: frame every element's bbox on screen (Set-of-Marks), color by role
      cursor-hud                     Persistent floating Persome cursor; reads newline-JSON {x,y,note,elements?} on stdin.
                                     A {"glow":{app,pid,state,note,task_id,point?}} line drives the takeover
                                     glow + badge on the driven window ({"glow":{"clear":true}} clears)
      act --verb press    --id ID    AXPress an element
      act --verb setvalue --id ID --text T   Set an element's AXValue
      act --verb confirm  --id ID    AXConfirm
      act --verb action   --id ID --text AXNAME   Perform an arbitrary AX action the element advertises
                                                  (AXIncrement/AXDecrement/AXShowMenu/AXPick/…) — for
                                                  steppers/date-pickers/dropdowns that ignore AXPress
      act --verb key   --keys "enter"|"cmd+k"     CGEvent key combo
      act --verb type  --text "..."               CGEvent Unicode typing (incl. Chinese) into the focused field
      act --verb clickxy --x N --y N              CGEvent left-click at a screen coord (top-left origin)
      act ... [--no-cursor]          Don't flash the Persome cursor at the action point (default: show)
      act ... [--show-boxes]         Also frame the app's element bboxes during the act (default: off)
      act ... [--note "..."]         Short "what Persome is doing this step" text, shown in the cursor bubble
      act ... [--feedback-seconds N] How long the cursor/boxes stay (default 0.6)
      trust                          Print AX-trust status (exit 0 trusted / 1 not)
      [--depth N]                    Max traversal depth (default 60)
    \n
    """.utf8))
    exit(64)
}
