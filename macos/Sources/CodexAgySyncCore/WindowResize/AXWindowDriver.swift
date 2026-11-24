import AppKit
import ApplicationServices
import Carbon.HIToolbox
import CoreGraphics
import Foundation
import os

public protocol WindowSystemDriving: Sendable {
    func windowAtPointer(_ point: CGPoint, excludingProcessId: pid_t) -> WindowTargetSnapshot?
    func currentFrame(for windowId: UInt64) -> CGRect?
    func beginEnhancedUIGuard(pid: pid_t)
    func endEnhancedUIGuard(pid: pid_t)
    @discardableResult
    func moveWindow(windowId: UInt64, to origin: CGPoint) -> Bool
    @discardableResult
    func resizeWindow(windowId: UInt64, to frame: CGRect, corner: ResizeAnchorCorner) -> Bool
    func performNativePlacement(_ action: WindowPlacementAction) -> WindowResizeActionResult
    func performSplitOrFullScreen(_ action: SplitScreenGestureAction, windowId: UInt64) -> WindowResizeActionResult
}

struct CachedAXWindow: @unchecked Sendable {
    let element: AXUIElement
    let pid: pid_t
}

enum NativeTilingBridge {
    private static let enhancedUIAttribute = "AXEnhancedUserInterface"
    private static let fullScreenAttribute = "AXFullScreen"
    private static let showMenuAction = "AXShowMenu"
    private static let cancelAction = "AXCancel"

    private static let windowMenuTitles: Set<String> = [
        "window",
        "窗口",
        "視窗",
        "fenster",
        "fenêtre",
        "ventana",
        "ウインドウ",
        "윈도우",
    ]

    private static let moveAndResizeSubmenuTitles: [String] = [
        "move & resize",
        "move and resize",
        "移动并调整大小",
        "移动与调整大小",
        "移動並調整大小",
        "移動與調整大小",
    ]

    private static let fullScreenTileSubmenuTitles: [String] = [
        "full screen tile",
        "全屏拼贴",
        "全螢幕並排",
        "全屏平铺",
    ]

    static func performNativePlacement(_ action: WindowPlacementAction) -> WindowResizeActionResult {
        guard AXIsProcessTrusted() else {
            return .permissionDenied
        }
        let macOS15 = OperatingSystemVersion(majorVersion: 15, minorVersion: 0, patchVersion: 0)
        guard ProcessInfo.processInfo.isOperatingSystemAtLeast(macOS15) else {
            return .unsupportedBySystemState(detail: "macOS 15+ is required for native Move & Resize")
        }
        guard let frontmost = NSWorkspace.shared.frontmostApplication else {
            return .unsupportedByTargetApp(detail: "No active frontmost application")
        }

        let appElement = AXUIElementCreateApplication(frontmost.processIdentifier)
        guard let menuBar = copyElementAttribute(kAXMenuBarAttribute as CFString, from: appElement) else {
            return .unsupportedByTargetApp(detail: "Frontmost app does not expose AXMenuBar")
        }

        let topMenus = copyElementArrayAttribute(kAXChildrenAttribute as CFString, from: menuBar)
        guard !topMenus.isEmpty else {
            return .unsupportedByTargetApp(detail: "Frontmost app menu bar is empty")
        }

        let candidateWindowMenus = findWindowMenus(in: topMenus)
        var foundDisabledMatch = false

        for menu in candidateWindowMenus {
            let outcome = searchAndPressPlacement(action: action, inMenu: menu)
            switch outcome {
            case .pressed:
                return .applied
            case .foundDisabled:
                foundDisabledMatch = true
            case .notFound:
                continue
            }
        }

        if foundDisabledMatch {
            return .unsupportedBySystemState(detail: "Native Move & Resize item is currently disabled")
        }
        return .unsupportedByTargetApp(detail: "Frontmost app does not expose native Window -> Move & Resize menu")
    }

    static func performSplitOrFullScreen(
        _ action: SplitScreenGestureAction,
        target: CachedAXWindow
    ) -> WindowResizeActionResult {
        guard AXIsProcessTrusted() else {
            return .permissionDenied
        }

        switch action {
        case .enterFullScreen:
            return enterFullScreen(window: target.element)
        case .exitFullScreenOrSplit:
            return exitFullScreenOrSplit(target: target)
        case .splitLeft, .splitRight:
            return enterSplitView(action: action, target: target)
        }
    }

    static func setEnhancedUserInterface(_ enabled: Bool, for pid: pid_t) -> Bool {
        let appElement = AXUIElementCreateApplication(pid)
        let err = AXUIElementSetAttributeValue(
            appElement,
            enhancedUIAttribute as CFString,
            enabled ? kCFBooleanTrue : kCFBooleanFalse
        )
        return err == .success
    }

    static func isEnhancedUserInterfaceEnabled(for pid: pid_t) -> Bool {
        let appElement = AXUIElementCreateApplication(pid)
        return copyBoolAttribute(enhancedUIAttribute as CFString, from: appElement) ?? false
    }

    static func isWindowFullScreen(_ window: AXUIElement) -> Bool {
        copyBoolAttribute(fullScreenAttribute as CFString, from: window) ?? false
    }

    private enum MenuPressOutcome {
        case pressed
        case foundDisabled
        case notFound
    }

    private static func findWindowMenus(in topMenuBarItems: [AXUIElement]) -> [AXUIElement] {
        var prioritized: [AXUIElement] = []
        var fallback: [AXUIElement] = []

        for barItem in topMenuBarItems {
            let title = normalizedTitle(of: barItem)
            let submenus = copyElementArrayAttribute(kAXChildrenAttribute as CFString, from: barItem)
            guard let menu = submenus.first else { continue }
            if windowMenuTitles.contains(title) {
                prioritized.append(menu)
            } else {
                fallback.append(menu)
            }
        }

        for menu in fallback where containsMoveAndResizeSubmenu(in: menu) {
            prioritized.append(menu)
        }
        return prioritized
    }

    private static func containsMoveAndResizeSubmenu(in menu: AXUIElement) -> Bool {
        let items = copyElementArrayAttribute(kAXChildrenAttribute as CFString, from: menu)
        for item in items {
            let title = normalizedTitle(of: item)
            if moveAndResizeSubmenuTitles.contains(where: { title == $0 || title.contains($0) }) {
                return true
            }
        }
        return false
    }

    private static func searchAndPressPlacement(
        action: WindowPlacementAction,
        inMenu windowMenu: AXUIElement
    ) -> MenuPressOutcome {
        let items = copyElementArrayAttribute(kAXChildrenAttribute as CFString, from: windowMenu)
        var moveResizeSubmenuItems: [AXUIElement] = []
        var directItems: [AXUIElement] = []

        for item in items {
            let title = normalizedTitle(of: item)
            if moveAndResizeSubmenuTitles.contains(where: { title == $0 || title.contains($0) }) {
                if let submenu = copyElementArrayAttribute(kAXChildrenAttribute as CFString, from: item).first {
                    let subItems = copyElementArrayAttribute(kAXChildrenAttribute as CFString, from: submenu)
                    moveResizeSubmenuItems.append(contentsOf: subItems)
                }
            } else {
                directItems.append(item)
            }
        }

        let orderedBuckets = [moveResizeSubmenuItems, directItems]
        var sawDisabled = false

        for bucket in orderedBuckets {
            if let match = matchPlacementItemByShortcut(action: action, in: bucket) {
                let outcome = pressIfEnabled(match)
                if outcome == .pressed { return .pressed }
                if outcome == .foundDisabled { sawDisabled = true }
            }
        }

        if let match = matchPlacementItemByTitle(action: action, in: moveResizeSubmenuItems) {
            let outcome = pressIfEnabled(match)
            if outcome == .pressed { return .pressed }
            if outcome == .foundDisabled { sawDisabled = true }
        }

        return sawDisabled ? .foundDisabled : .notFound
    }

    private static func matchPlacementItemByShortcut(
        action: WindowPlacementAction,
        in items: [AXUIElement]
    ) -> AXUIElement? {
        for item in items {
            let rawModifiers = copyIntAttribute(kAXMenuItemCmdModifiersAttribute as CFString, from: item) ?? 0
            let hasControl = (rawModifiers & 4) != 0
            let hasShift = (rawModifiers & 1) != 0
            let hasOption = (rawModifiers & 2) != 0
            let hasNoCommand = (rawModifiers & 8) != 0
            guard hasControl, hasNoCommand, !hasShift, !hasOption else {
                continue
            }

            if let virtualKey = copyIntAttribute(kAXMenuItemCmdVirtualKeyAttribute as CFString, from: item),
               virtualKey == Int(action.keyCode)
            {
                return item
            }

            if let cmdChar = copyStringAttribute(kAXMenuItemCmdCharAttribute as CFString, from: item)?
                .trimmingCharacters(in: .whitespacesAndNewlines)
                .uppercased(),
               let expectedChar = expectedCommandChar(for: action),
               cmdChar == expectedChar
            {
                return item
            }
        }
        return nil
    }

    private static func expectedCommandChar(for action: WindowPlacementAction) -> String? {
        switch action {
        case .fill:
            return "F"
        case .center:
            return "C"
        case .restorePrevious:
            return "R"
        case .tileLeft, .tileRight, .tileTop, .tileBottom:
            return nil
        }
    }

    private static func matchPlacementItemByTitle(
        action: WindowPlacementAction,
        in items: [AXUIElement]
    ) -> AXUIElement? {
        let candidates = localizedPlacementTitles(for: action)
        for item in items {
            let title = normalizedTitle(of: item)
            guard !title.isEmpty else { continue }
            if candidates.exact.contains(title) {
                return item
            }
        }
        for item in items {
            let title = normalizedTitle(of: item)
            guard !title.isEmpty else { continue }
            if candidates.contains.contains(where: { title.contains($0) }) {
                return item
            }
        }
        return nil
    }

    private static func localizedPlacementTitles(
        for action: WindowPlacementAction
    ) -> (exact: Set<String>, contains: [String]) {
        switch action {
        case .tileLeft:
            return (["left", "左", "左侧", "左側"], ["left half"])
        case .tileRight:
            return (["right", "右", "右侧", "右側"], ["right half"])
        case .tileTop:
            return (["top", "上", "顶部", "頂部"], ["top half"])
        case .tileBottom:
            return (["bottom", "下", "底部"], ["bottom half"])
        case .fill:
            return (["fill", "填充", "填满", "填滿"], ["fill screen", "填充屏幕"])
        case .center:
            return (["center", "居中", "置中"], ["center window", "居中窗口"])
        case .restorePrevious:
            return (
                ["return to previous size", "恢复之前的大小", "还原之前的大小", "恢復之前的大小", "恢复原大小"],
                ["previous size", "之前的大小"]
            )
        }
    }

    private static func enterFullScreen(window: AXUIElement) -> WindowResizeActionResult {
        if AXUIElementSetAttributeValue(window, fullScreenAttribute as CFString, kCFBooleanTrue) == .success {
            return .applied
        }
        if let button = copyElementAttribute(kAXFullScreenButtonAttribute as CFString, from: window),
           AXUIElementPerformAction(button, kAXPressAction as CFString) == .success
        {
            return .applied
        }
        return .unsupportedByTargetApp(detail: "Target window does not support native full screen")
    }

    private static func exitFullScreenOrSplit(target: CachedAXWindow) -> WindowResizeActionResult {
        let targetDisplayFrame = displayFrame(for: target.element)
        var exitedTarget = false

        if isWindowFullScreen(target.element) {
            if AXUIElementSetAttributeValue(target.element, fullScreenAttribute as CFString, kCFBooleanFalse) == .success {
                exitedTarget = true
            } else if let button = copyElementAttribute(kAXFullScreenButtonAttribute as CFString, from: target.element),
                      AXUIElementPerformAction(button, kAXPressAction as CFString) == .success
            {
                exitedTarget = true
            }
        }

        let pairedExited = exitPairedFullScreenWindows(
            excludingWindow: target.element,
            displayFrame: targetDisplayFrame
        )

        if exitedTarget || pairedExited {
            return .applied
        }
        return .unsupportedBySystemState(detail: "Target window is not in full screen or Split View")
    }

    private static func exitPairedFullScreenWindows(
        excludingWindow: AXUIElement,
        displayFrame: CGRect?
    ) -> Bool {
        guard let infoList = CGWindowListCopyWindowInfo(
            [.optionOnScreenOnly, .excludeDesktopElements],
            kCGNullWindowID
        ) as? [[String: Any]] else {
            return false
        }

        var inspectedPids: Set<pid_t> = []
        var anyExited = false

        for info in infoList {
            guard let layer = info[kCGWindowLayer as String] as? Int, layer == 0,
                  let pid = info[kCGWindowOwnerPID as String] as? pid_t
            else {
                continue
            }
            if let displayFrame,
               let boundsDict = info[kCGWindowBounds as String] as? NSDictionary,
               let bounds = CGRect(dictionaryRepresentation: boundsDict),
               !bounds.intersects(displayFrame)
            {
                continue
            }
            guard inspectedPids.insert(pid).inserted else { continue }
            let appElement = AXUIElementCreateApplication(pid)
            let windows = copyElementArrayAttribute(kAXWindowsAttribute as CFString, from: appElement)
            for candidate in windows {
                if CFEqual(candidate, excludingWindow) {
                    continue
                }
                guard isWindowFullScreen(candidate) else { continue }
                if let displayFrame, let candidateFrame = SystemAXWindowDriver.readFrame(of: candidate),
                   !candidateFrame.intersects(displayFrame)
                {
                    continue
                }
                if AXUIElementSetAttributeValue(candidate, fullScreenAttribute as CFString, kCFBooleanFalse) == .success {
                    anyExited = true
                } else if let button = copyElementAttribute(kAXFullScreenButtonAttribute as CFString, from: candidate),
                          AXUIElementPerformAction(button, kAXPressAction as CFString) == .success
                {
                    anyExited = true
                }
            }
        }

        return anyExited
    }

    private static func enterSplitView(
        action: SplitScreenGestureAction,
        target: CachedAXWindow
    ) -> WindowResizeActionResult {
        let isLeft = (action == .splitLeft)
        var foundDisabled = false

        if let button = copyElementAttribute(kAXFullScreenButtonAttribute as CFString, from: target.element) {
            if AXUIElementPerformAction(button, showMenuAction as CFString) == .success {
                let outcome = pressSplitMenuItem(inContainer: button, isLeft: isLeft)
                switch outcome {
                case .pressed:
                    return .applied
                case .foundDisabled:
                    foundDisabled = true
                    dismissMenuIfOpen(inContainer: button)
                case .notFound:
                    dismissMenuIfOpen(inContainer: button)
                }
            }
        }

        let appElement = AXUIElementCreateApplication(target.pid)
        if let menuBar = copyElementAttribute(kAXMenuBarAttribute as CFString, from: appElement) {
            let topMenus = copyElementArrayAttribute(kAXChildrenAttribute as CFString, from: menuBar)
            let windowMenus = findWindowMenus(in: topMenus)
            for menu in windowMenus {
                let outcome = pressSplitMenuItem(inContainer: menu, isLeft: isLeft)
                switch outcome {
                case .pressed:
                    return .applied
                case .foundDisabled:
                    foundDisabled = true
                case .notFound:
                    continue
                }
            }
        }

        if foundDisabled {
            return .unsupportedBySystemState(detail: "Native Split View is disabled by current system state")
        }
        return .unsupportedByTargetApp(detail: "Target window does not expose native Split View tiling")
    }

    private static func pressSplitMenuItem(inContainer root: AXUIElement, isLeft: Bool) -> MenuPressOutcome {
        let menuItems = collectMenuItems(under: root, maxDepth: 4)
        let exactTitles: Set<String> = isLeft
            ? [
                "tile window to left of screen",
                "left of screen",
                "将窗口拼贴到屏幕左侧",
                "屏幕左侧",
                "將視窗並排至螢幕左側",
                "螢幕左側",
            ]
            : [
                "tile window to right of screen",
                "right of screen",
                "将窗口拼贴到屏幕右侧",
                "屏幕右侧",
                "將視窗並排至螢幕右側",
                "螢幕右側",
            ]
        let substringPatterns: [String] = isLeft
            ? ["left of screen", "屏幕左侧", "螢幕左側"]
            : ["right of screen", "屏幕右侧", "螢幕右側"]

        var sawDisabled = false

        for item in menuItems {
            let title = normalizedTitle(of: item)
            if exactTitles.contains(title) {
                let outcome = pressIfEnabled(item)
                if outcome == .pressed { return .pressed }
                if outcome == .foundDisabled { sawDisabled = true }
            }
        }

        for item in menuItems {
            let title = normalizedTitle(of: item)
            if substringPatterns.contains(where: { title.contains($0) }) {
                let outcome = pressIfEnabled(item)
                if outcome == .pressed { return .pressed }
                if outcome == .foundDisabled { sawDisabled = true }
            }
        }

        return sawDisabled ? .foundDisabled : .notFound
    }

    private static func collectMenuItems(under root: AXUIElement, maxDepth: Int) -> [AXUIElement] {
        guard maxDepth > 0 else { return [] }
        var collected: [AXUIElement] = []
        let children = copyElementArrayAttribute(kAXChildrenAttribute as CFString, from: root)
        for child in children {
            let role = copyStringAttribute(kAXRoleAttribute as CFString, from: child)
            let title = normalizedTitle(of: child)
            if role == (kAXMenuItemRole as String) {
                collected.append(child)
                if fullScreenTileSubmenuTitles.contains(where: { title.contains($0) })
                    || title.contains("tile")
                    || title.contains("拼贴")
                    || title.contains("並排")
                {
                    collected.append(contentsOf: collectMenuItems(under: child, maxDepth: maxDepth - 1))
                }
            } else {
                collected.append(contentsOf: collectMenuItems(under: child, maxDepth: maxDepth - 1))
            }
        }
        return collected
    }

    private static func dismissMenuIfOpen(inContainer container: AXUIElement) {
        let children = copyElementArrayAttribute(kAXChildrenAttribute as CFString, from: container)
        for child in children {
            _ = AXUIElementPerformAction(child, cancelAction as CFString)
        }
    }

    private static func pressIfEnabled(_ item: AXUIElement) -> MenuPressOutcome {
        let enabled = copyBoolAttribute(kAXEnabledAttribute as CFString, from: item) ?? true
        guard enabled else {
            return .foundDisabled
        }
        let err = AXUIElementPerformAction(item, kAXPressAction as CFString)
        return err == .success ? .pressed : .foundDisabled
    }

    private static func displayFrame(for window: AXUIElement) -> CGRect? {
        guard let frame = SystemAXWindowDriver.readFrame(of: window) else {
            return nil
        }
        var displayCount: UInt32 = 0
        var displayId: CGDirectDisplayID = 0
        let err = CGGetDisplaysWithRect(frame, 1, &displayId, &displayCount)
        if err == .success, displayCount > 0 {
            return CGDisplayBounds(displayId)
        }
        return CGDisplayBounds(CGMainDisplayID())
    }

    static func copyElementAttribute(_ attribute: CFString, from element: AXUIElement) -> AXUIElement? {
        var value: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, attribute, &value) == .success,
              let value,
              CFGetTypeID(value) == AXUIElementGetTypeID()
        else {
            return nil
        }
        return unsafeDowncast(value, to: AXUIElement.self)
    }

    static func copyElementArrayAttribute(_ attribute: CFString, from element: AXUIElement) -> [AXUIElement] {
        var value: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, attribute, &value) == .success,
              let array = value as? [AXUIElement]
        else {
            return []
        }
        return array
    }

    static func copyStringAttribute(_ attribute: CFString, from element: AXUIElement) -> String? {
        var value: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, attribute, &value) == .success else {
            return nil
        }
        return value as? String
    }

    static func copyBoolAttribute(_ attribute: CFString, from element: AXUIElement) -> Bool? {
        var value: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, attribute, &value) == .success else {
            return nil
        }
        if let boolValue = value as? Bool {
            return boolValue
        }
        if let number = value as? NSNumber {
            return number.boolValue
        }
        return nil
    }

    static func copyIntAttribute(_ attribute: CFString, from element: AXUIElement) -> Int? {
        var value: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, attribute, &value) == .success else {
            return nil
        }
        if let number = value as? NSNumber {
            return number.intValue
        }
        return nil
    }

    private static func normalizedTitle(of element: AXUIElement) -> String {
        (copyStringAttribute(kAXTitleAttribute as CFString, from: element) ?? "")
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .lowercased()
    }
}

public final class SystemAXWindowDriver: WindowSystemDriving, @unchecked Sendable {
    private struct CacheState: @unchecked Sendable {
        var windowsById: [UInt64: CachedAXWindow] = [:]
        var insertionOrder: [UInt64] = []
        var nextSyntheticId: UInt64 = 1
        var disabledEnhancedUIPids: Set<pid_t> = []
    }

    private static let maxCachedWindows = 64
    private static let ignoredSubroles: Set<String> = [
        "AXSheet",
        kAXSystemDialogSubrole as String,
    ]
    private static let windowNumberAttribute = "AXWindowNumber"

    private let systemWide = AXUIElementCreateSystemWide()
    private let lock = OSAllocatedUnfairLock(initialState: CacheState())

    public init() {}

    public func windowAtPointer(_ point: CGPoint, excludingProcessId: pid_t) -> WindowTargetSnapshot? {
        guard AXIsProcessTrusted() else {
            return nil
        }

        if let resolved = resolveViaElementAtPosition(point, excludingProcessId: excludingProcessId) {
            return makeSnapshot(element: resolved.element, pid: resolved.pid)
        }

        if let resolved = resolveViaWindowList(point, excludingProcessId: excludingProcessId) {
            return makeSnapshot(element: resolved.element, pid: resolved.pid)
        }

        return nil
    }

    public func currentFrame(for windowId: UInt64) -> CGRect? {
        guard let cached = lock.withLock({ $0.windowsById[windowId] }) else {
            return nil
        }
        return Self.readFrame(of: cached.element)
    }

    public func beginEnhancedUIGuard(pid: pid_t) {
        guard AXIsProcessTrusted() else { return }
        let alreadyGuarded = lock.withLock { $0.disabledEnhancedUIPids.contains(pid) }
        guard !alreadyGuarded else { return }
        guard NativeTilingBridge.isEnhancedUserInterfaceEnabled(for: pid) else { return }
        if NativeTilingBridge.setEnhancedUserInterface(false, for: pid) {
            lock.withLock {
                _ = $0.disabledEnhancedUIPids.insert(pid)
            }
        }
    }

    public func endEnhancedUIGuard(pid: pid_t) {
        let shouldRestore = lock.withLock {
            $0.disabledEnhancedUIPids.remove(pid) != nil
        }
        guard shouldRestore else { return }
        _ = NativeTilingBridge.setEnhancedUserInterface(true, for: pid)
    }

    @discardableResult
    public func moveWindow(windowId: UInt64, to origin: CGPoint) -> Bool {
        guard let cached = lock.withLock({ $0.windowsById[windowId] }) else {
            return false
        }
        return Self.writeOrigin(origin, to: cached.element)
    }

    @discardableResult
    public func resizeWindow(windowId: UInt64, to frame: CGRect, corner: ResizeAnchorCorner) -> Bool {
        guard let cached = lock.withLock({ $0.windowsById[windowId] }) else {
            return false
        }
        if corner == .bottomRight {
            return Self.writeSize(frame.size, to: cached.element)
        }

        _ = Self.writeOrigin(frame.origin, to: cached.element)
        let sizeOk = Self.writeSize(frame.size, to: cached.element)
        if let actualSize = Self.readSize(of: cached.element) {
            let adjusted = WindowGeometry.adjustOriginAfterActualSize(
                desiredFrame: frame,
                actualSize: actualSize,
                corner: corner
            )
            if abs(adjusted.x - frame.origin.x) > 0.5 || abs(adjusted.y - frame.origin.y) > 0.5 {
                _ = Self.writeOrigin(adjusted, to: cached.element)
            }
        }
        return sizeOk
    }

    public func performNativePlacement(_ action: WindowPlacementAction) -> WindowResizeActionResult {
        NativeTilingBridge.performNativePlacement(action)
    }

    public func performSplitOrFullScreen(
        _ action: SplitScreenGestureAction,
        windowId: UInt64
    ) -> WindowResizeActionResult {
        guard let cached = lock.withLock({ $0.windowsById[windowId] }) else {
            return .unsupportedByTargetApp(detail: "Target window is no longer available")
        }
        return NativeTilingBridge.performSplitOrFullScreen(action, target: cached)
    }

    private func resolveViaElementAtPosition(
        _ point: CGPoint,
        excludingProcessId: pid_t
    ) -> CachedAXWindow? {
        var hitElement: AXUIElement?
        let status = AXUIElementCopyElementAtPosition(
            systemWide,
            Float(point.x),
            Float(point.y),
            &hitElement
        )
        guard status == .success, let startElement = hitElement else {
            return nil
        }

        guard let windowElement = ascendToWindow(from: startElement) else {
            return nil
        }

        var pid: pid_t = 0
        guard AXUIElementGetPid(windowElement, &pid) == .success,
              pid > 0,
              pid != excludingProcessId
        else {
            return nil
        }
        if let subrole = NativeTilingBridge.copyStringAttribute(kAXSubroleAttribute as CFString, from: windowElement),
           Self.ignoredSubroles.contains(subrole)
        {
            return nil
        }
        return CachedAXWindow(element: windowElement, pid: pid)
    }

    private func ascendToWindow(from element: AXUIElement) -> AXUIElement? {
        if let directWindow = NativeTilingBridge.copyElementAttribute(kAXWindowAttribute as CFString, from: element),
           isWindowElement(directWindow)
        {
            return directWindow
        }

        var current: AXUIElement? = element
        for _ in 0..<12 {
            guard let candidate = current else { break }
            if isWindowElement(candidate) {
                return candidate
            }
            current = NativeTilingBridge.copyElementAttribute(kAXParentAttribute as CFString, from: candidate)
        }
        return nil
    }

    private func isWindowElement(_ element: AXUIElement) -> Bool {
        NativeTilingBridge.copyStringAttribute(kAXRoleAttribute as CFString, from: element) == (kAXWindowRole as String)
    }

    private func resolveViaWindowList(
        _ point: CGPoint,
        excludingProcessId: pid_t
    ) -> CachedAXWindow? {
        guard let infoList = CGWindowListCopyWindowInfo(
            [.optionOnScreenOnly, .excludeDesktopElements],
            kCGNullWindowID
        ) as? [[String: Any]] else {
            return nil
        }

        for info in infoList {
            guard let layer = info[kCGWindowLayer as String] as? Int, layer == 0,
                  let pid = info[kCGWindowOwnerPID as String] as? pid_t,
                  pid > 0,
                  pid != excludingProcessId,
                  let boundsDict = info[kCGWindowBounds as String] as? NSDictionary,
                  let bounds = CGRect(dictionaryRepresentation: boundsDict),
                  bounds.contains(point)
            else {
                continue
            }

            let targetWindowNumber = info[kCGWindowNumber as String] as? Int
            let appElement = AXUIElementCreateApplication(pid)
            let windows = NativeTilingBridge.copyElementArrayAttribute(
                kAXWindowsAttribute as CFString,
                from: appElement
            )
            if let matched = matchAXWindow(
                in: windows,
                windowNumber: targetWindowNumber,
                bounds: bounds,
                point: point
            ) {
                return CachedAXWindow(element: matched, pid: pid)
            }
        }
        return nil
    }

    private func matchAXWindow(
        in windows: [AXUIElement],
        windowNumber: Int?,
        bounds: CGRect,
        point: CGPoint
    ) -> AXUIElement? {
        if let windowNumber {
            for candidate in windows {
                if NativeTilingBridge.copyIntAttribute(Self.windowNumberAttribute as CFString, from: candidate) == windowNumber {
                    return candidate
                }
            }
        }
        for candidate in windows {
            guard let frame = Self.readFrame(of: candidate) else { continue }
            let dx = abs(frame.origin.x - bounds.origin.x)
            let dy = abs(frame.origin.y - bounds.origin.y)
            let dw = abs(frame.size.width - bounds.size.width)
            let dh = abs(frame.size.height - bounds.size.height)
            if dx <= 2, dy <= 2, dw <= 2, dh <= 2 {
                return candidate
            }
        }
        for candidate in windows {
            if let frame = Self.readFrame(of: candidate), frame.contains(point) {
                return candidate
            }
        }
        return nil
    }

    private func makeSnapshot(element: AXUIElement, pid: pid_t) -> WindowTargetSnapshot? {
        guard let frame = Self.readFrame(of: element) else {
            return nil
        }
        var movable: DarwinBoolean = false
        let movableStatus = AXUIElementIsAttributeSettable(element, kAXPositionAttribute as CFString, &movable)
        var resizable: DarwinBoolean = false
        let resizableStatus = AXUIElementIsAttributeSettable(element, kAXSizeAttribute as CFString, &resizable)

        let windowId = cacheWindow(CachedAXWindow(element: element, pid: pid))
        let bundleId = NSRunningApplication(processIdentifier: pid)?.bundleIdentifier
        return WindowTargetSnapshot(
            windowId: windowId,
            pid: pid,
            bundleId: bundleId,
            frame: frame,
            isMovable: movableStatus == .success && movable.boolValue,
            isResizable: resizableStatus == .success && resizable.boolValue,
            isFullScreen: NativeTilingBridge.isWindowFullScreen(element)
        )
    }

    private func cacheWindow(_ entry: CachedAXWindow) -> UInt64 {
        let nativeNumber = NativeTilingBridge.copyIntAttribute(
            Self.windowNumberAttribute as CFString,
            from: entry.element
        )
        return lock.withLock { state in
            let id: UInt64
            if let nativeNumber, nativeNumber > 0 {
                id = UInt64(nativeNumber)
            } else {
                id = (1 << 48) | state.nextSyntheticId
                state.nextSyntheticId += 1
            }
            if state.windowsById[id] == nil {
                state.insertionOrder.append(id)
            }
            state.windowsById[id] = entry
            while state.insertionOrder.count > Self.maxCachedWindows {
                let evicted = state.insertionOrder.removeFirst()
                state.windowsById.removeValue(forKey: evicted)
            }
            return id
        }
    }

    static func readFrame(of element: AXUIElement) -> CGRect? {
        guard let origin = readOrigin(of: element),
              let size = readSize(of: element)
        else {
            return nil
        }
        return CGRect(origin: origin, size: size)
    }

    static func readOrigin(of element: AXUIElement) -> CGPoint? {
        var value: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, kAXPositionAttribute as CFString, &value) == .success,
              let value,
              CFGetTypeID(value) == AXValueGetTypeID()
        else {
            return nil
        }
        let axValue = unsafeDowncast(value, to: AXValue.self)
        var point = CGPoint.zero
        guard AXValueGetType(axValue) == .cgPoint, AXValueGetValue(axValue, .cgPoint, &point) else {
            return nil
        }
        return point
    }

    static func readSize(of element: AXUIElement) -> CGSize? {
        var value: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, kAXSizeAttribute as CFString, &value) == .success,
              let value,
              CFGetTypeID(value) == AXValueGetTypeID()
        else {
            return nil
        }
        let axValue = unsafeDowncast(value, to: AXValue.self)
        var size = CGSize.zero
        guard AXValueGetType(axValue) == .cgSize, AXValueGetValue(axValue, .cgSize, &size) else {
            return nil
        }
        return size
    }

    static func writeOrigin(_ origin: CGPoint, to element: AXUIElement) -> Bool {
        var mutableOrigin = origin
        guard let value = AXValueCreate(.cgPoint, &mutableOrigin) else {
            return false
        }
        return AXUIElementSetAttributeValue(element, kAXPositionAttribute as CFString, value) == .success
    }

    static func writeSize(_ size: CGSize, to element: AXUIElement) -> Bool {
        var mutableSize = size
        guard let value = AXValueCreate(.cgSize, &mutableSize) else {
            return false
        }
        return AXUIElementSetAttributeValue(element, kAXSizeAttribute as CFString, value) == .success
    }
}
