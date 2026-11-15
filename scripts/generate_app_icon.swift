#!/usr/bin/env swift
import AppKit
import CoreGraphics
import Foundation

enum IconGenError: LocalizedError {
    case invalidArguments
    case forbiddenTemporaryPath(String)
    case bitmapCreationFailed(Int)
    case pngEncodingFailed(String)
    case iconutilFailed(Int32, String)
    case emptyOutput(String)

    var errorDescription: String? {
        switch self {
        case .invalidArguments:
            return "Usage: swift scripts/generate_app_icon.swift <output-icns-path> <scratch-dir>"
        case .forbiddenTemporaryPath(let detail):
            return "Refusing /tmp or /private/tmp path: \(detail)"
        case .bitmapCreationFailed(let px):
            return "Failed to allocate \(px)x\(px) NSBitmapImageRep"
        case .pngEncodingFailed(let name):
            return "Failed to encode PNG for \(name)"
        case .iconutilFailed(let code, let stderr):
            return "iconutil exited with \(code): \(stderr)"
        case .emptyOutput(let path):
            return "Generated .icns is missing or empty at \(path)"
        }
    }
}

func isForbiddenTmpString(_ path: String) -> Bool {
    path == "/tmp"
        || path.hasPrefix("/tmp/")
        || path == "/private/tmp"
        || path.hasPrefix("/private/tmp/")
}

func resolveRealAncestorPath(_ rawPath: String) -> String {
    let expanded = (rawPath as NSString).expandingTildeInPath
    let absolute: String
    if expanded.hasPrefix("/") {
        absolute = (expanded as NSString).standardizingPath
    } else {
        let cwd = FileManager.default.currentDirectoryPath
        absolute = ((cwd as NSString).appendingPathComponent(expanded) as NSString).standardizingPath
    }

    let fm = FileManager.default
    var probe = URL(fileURLWithPath: absolute)
    var trailingComponents: [String] = []
    while !fm.fileExists(atPath: probe.path) && probe.path != "/" {
        trailingComponents.append(probe.lastPathComponent)
        probe.deleteLastPathComponent()
    }
    var resolved = probe.resolvingSymlinksInPath()
    for component in trailingComponents.reversed() {
        resolved.appendPathComponent(component)
    }
    return (resolved.path as NSString).standardizingPath
}

func validateNonTemporaryPath(_ rawPath: String, label: String) throws -> URL {
    let trimmed = rawPath.trimmingCharacters(in: .whitespacesAndNewlines)
    guard !trimmed.isEmpty else {
        throw IconGenError.invalidArguments
    }
    let expanded = ((trimmed as NSString).expandingTildeInPath as NSString).standardizingPath
    if isForbiddenTmpString(expanded) {
        throw IconGenError.forbiddenTemporaryPath("\(label)=\(rawPath)")
    }
    let resolved = resolveRealAncestorPath(trimmed)
    if isForbiddenTmpString(resolved) {
        throw IconGenError.forbiddenTemporaryPath("\(label)=\(rawPath) (resolved: \(resolved))")
    }
    return URL(fileURLWithPath: resolved)
}

func srgb(_ hex: UInt32, alpha: CGFloat = 1.0) -> CGColor {
    let r = CGFloat((hex >> 16) & 0xFF) / 255.0
    let g = CGFloat((hex >> 8) & 0xFF) / 255.0
    let b = CGFloat(hex & 0xFF) / 255.0
    return CGColor(srgbRed: r, green: g, blue: b, alpha: alpha)
}

func makeLinearGradient(_ topHex: UInt32, _ bottomHex: UInt32, colorSpace: CGColorSpace) -> CGGradient {
    let colors = [srgb(topHex), srgb(bottomHex)] as CFArray
    let locations: [CGFloat] = [0.0, 1.0]
    return CGGradient(colorsSpace: colorSpace, colors: colors, locations: locations)!
}

func renderIconCanvas(px: Int) throws -> Data {
    guard let rep = NSBitmapImageRep(
        bitmapDataPlanes: nil,
        pixelsWide: px,
        pixelsHigh: px,
        bitsPerSample: 8,
        samplesPerPixel: 4,
        hasAlpha: true,
        isPlanar: false,
        colorSpaceName: .deviceRGB,
        bitmapFormat: [],
        bytesPerRow: 0,
        bitsPerPixel: 0
    ) else {
        throw IconGenError.bitmapCreationFailed(px)
    }
    rep.size = NSSize(width: px, height: px)

    guard let nsCtx = NSGraphicsContext(bitmapImageRep: rep) else {
        throw IconGenError.bitmapCreationFailed(px)
    }

    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = nsCtx
    defer { NSGraphicsContext.restoreGraphicsState() }

    let ctx = nsCtx.cgContext
    ctx.setAllowsAntialiasing(true)
    ctx.setShouldAntialias(true)
    ctx.interpolationQuality = .high
    ctx.clear(CGRect(x: 0, y: 0, width: px, height: px))

    let scale = CGFloat(px) / 1024.0
    ctx.scaleBy(x: scale, y: scale)

    let colorSpace = CGColorSpace(name: CGColorSpace.sRGB) ?? CGColorSpaceCreateDeviceRGB()
    let squircleRect = CGRect(x: 96, y: 96, width: 832, height: 832)
    let squirclePath = CGPath(roundedRect: squircleRect, cornerWidth: 186, cornerHeight: 186, transform: nil)

    if px >= 64 {
        ctx.saveGState()
        ctx.setShadow(
            offset: CGSize(width: 0, height: -14),
            blur: 28,
            color: CGColor(srgbRed: 0, green: 0, blue: 0, alpha: 0.38)
        )
        ctx.setFillColor(srgb(0x0F131C))
        ctx.addPath(squirclePath)
        ctx.fillPath()
        ctx.restoreGState()
    }

    ctx.saveGState()
    ctx.addPath(squirclePath)
    ctx.clip()

    let bgGradient = makeLinearGradient(0x1E2433, 0x0F131C, colorSpace: colorSpace)
    ctx.drawLinearGradient(
        bgGradient,
        start: CGPoint(x: 96, y: 928),
        end: CGPoint(x: 928, y: 96),
        options: [.drawsBeforeStartLocation, .drawsAfterEndLocation]
    )

    let railWidth: CGFloat = px <= 32 ? 128 : 112
    let railRadius = railWidth / 2.0
    let leftX: CGFloat = 324
    let rightX: CGFloat = 700
    let railBottomY: CGFloat = 244
    let railHeight: CGFloat = 536

    let leftRailRect = CGRect(x: leftX - railRadius, y: railBottomY, width: railWidth, height: railHeight)
    let leftRailPath = CGPath(roundedRect: leftRailRect, cornerWidth: railRadius, cornerHeight: railRadius, transform: nil)
    ctx.saveGState()
    ctx.addPath(leftRailPath)
    ctx.clip()
    ctx.drawLinearGradient(
        makeLinearGradient(0x60A5FA, 0x2563EB, colorSpace: colorSpace),
        start: CGPoint(x: leftX, y: railBottomY + railHeight),
        end: CGPoint(x: leftX, y: railBottomY),
        options: []
    )
    ctx.restoreGState()

    let rightRailRect = CGRect(x: rightX - railRadius, y: railBottomY, width: railWidth, height: railHeight)
    let rightRailPath = CGPath(roundedRect: rightRailRect, cornerWidth: railRadius, cornerHeight: railRadius, transform: nil)
    ctx.saveGState()
    ctx.addPath(rightRailPath)
    ctx.clip()
    ctx.drawLinearGradient(
        makeLinearGradient(0xFB923C, 0xEA580C, colorSpace: colorSpace),
        start: CGPoint(x: rightX, y: railBottomY + railHeight),
        end: CGPoint(x: rightX, y: railBottomY),
        options: []
    )
    ctx.restoreGState()

    let leftNode = CGPoint(x: leftX, y: 368)
    let rightNode = CGPoint(x: rightX, y: 656)
    let bridgePath = CGMutablePath()
    bridgePath.move(to: leftNode)
    bridgePath.addCurve(
        to: rightNode,
        control1: CGPoint(x: 504, y: 368),
        control2: CGPoint(x: 520, y: 656)
    )

    let bridgeWidth: CGFloat = px <= 32 ? 92 : 76
    let nodeOuterRadius: CGFloat = px <= 32 ? 70 : 60

    if px >= 64 {
        ctx.saveGState()
        ctx.setShadow(
            offset: CGSize(width: 0, height: -8),
            blur: 18,
            color: CGColor(srgbRed: 0, green: 0, blue: 0, alpha: 0.42)
        )
        ctx.setStrokeColor(srgb(0xFFFFFF))
        ctx.setLineWidth(bridgeWidth)
        ctx.setLineCap(.round)
        ctx.addPath(bridgePath)
        ctx.strokePath()
        ctx.restoreGState()
    } else {
        ctx.saveGState()
        ctx.setStrokeColor(srgb(0xFFFFFF))
        ctx.setLineWidth(bridgeWidth)
        ctx.setLineCap(.round)
        ctx.addPath(bridgePath)
        ctx.strokePath()
        ctx.restoreGState()
    }

    for (center, coreHex) in [(leftNode, UInt32(0x2563EB)), (rightNode, UInt32(0xEA580C))] {
        let outerRect = CGRect(
            x: center.x - nodeOuterRadius,
            y: center.y - nodeOuterRadius,
            width: nodeOuterRadius * 2,
            height: nodeOuterRadius * 2
        )
        ctx.setFillColor(srgb(0xFFFFFF))
        ctx.fillEllipse(in: outerRect)

        if px >= 64 {
            let innerRadius: CGFloat = 24
            let innerRect = CGRect(
                x: center.x - innerRadius,
                y: center.y - innerRadius,
                width: innerRadius * 2,
                height: innerRadius * 2
            )
            ctx.setFillColor(srgb(coreHex))
            ctx.fillEllipse(in: innerRect)
        }
    }

    let rimInset: CGFloat = 5
    let rimRect = squircleRect.insetBy(dx: rimInset, dy: rimInset)
    let rimPath = CGPath(roundedRect: rimRect, cornerWidth: 181, cornerHeight: 181, transform: nil)
    ctx.setStrokeColor(srgb(0x334155, alpha: 0.85))
    ctx.setLineWidth(10)
    ctx.addPath(rimPath)
    ctx.strokePath()

    ctx.restoreGState()

    guard let pngData = rep.representation(using: .png, properties: [:]) else {
        throw IconGenError.pngEncodingFailed("\(px)x\(px)")
    }
    return pngData
}

func main() throws {
    let args = Array(CommandLine.arguments.dropFirst())
    guard args.count == 2 else {
        throw IconGenError.invalidArguments
    }

    let outputICNSURL = try validateNonTemporaryPath(args[0], label: "output-icns-path")
    let scratchDirURL = try validateNonTemporaryPath(args[1], label: "scratch-dir")

    let fm = FileManager.default
    let iconsetURL = scratchDirURL.appendingPathComponent("AppIcon.iconset", isDirectory: true)
    if fm.fileExists(atPath: iconsetURL.path) {
        try fm.removeItem(at: iconsetURL)
    }
    try fm.createDirectory(at: iconsetURL, withIntermediateDirectories: true)
    try fm.createDirectory(
        at: outputICNSURL.deletingLastPathComponent(),
        withIntermediateDirectories: true
    )

    let specs: [(px: Int, filename: String)] = [
        (16, "icon_16x16.png"),
        (32, "icon_16x16@2x.png"),
        (32, "icon_32x32.png"),
        (64, "icon_32x32@2x.png"),
        (128, "icon_128x128.png"),
        (256, "icon_128x128@2x.png"),
        (256, "icon_256x256.png"),
        (512, "icon_256x256@2x.png"),
        (512, "icon_512x512.png"),
        (1024, "icon_512x512@2x.png"),
    ]

    var cache: [Int: Data] = [:]
    for spec in specs {
        let pngData: Data
        if let cached = cache[spec.px] {
            pngData = cached
        } else {
            let rendered = try renderIconCanvas(px: spec.px)
            cache[spec.px] = rendered
            pngData = rendered
        }
        try pngData.write(to: iconsetURL.appendingPathComponent(spec.filename), options: .atomic)
    }

    if fm.fileExists(atPath: outputICNSURL.path) {
        try fm.removeItem(at: outputICNSURL)
    }

    let proc = Process()
    proc.executableURL = URL(fileURLWithPath: "/usr/bin/iconutil")
    proc.arguments = ["-c", "icns", iconsetURL.path, "-o", outputICNSURL.path]
    let stderrPipe = Pipe()
    proc.standardError = stderrPipe
    try proc.run()
    let errData = stderrPipe.fileHandleForReading.readDataToEndOfFile()
    proc.waitUntilExit()

    if proc.terminationStatus != 0 {
        let stderr = String(data: errData, encoding: .utf8) ?? ""
        throw IconGenError.iconutilFailed(proc.terminationStatus, stderr.trimmingCharacters(in: .whitespacesAndNewlines))
    }

    let attrs = try fm.attributesOfItem(atPath: outputICNSURL.path)
    let fileSize = (attrs[.size] as? NSNumber)?.int64Value ?? 0
    guard fileSize > 0 else {
        throw IconGenError.emptyOutput(outputICNSURL.path)
    }
}

do {
    try main()
} catch {
    fputs("Error: \(error.localizedDescription)\n", stderr)
    exit(1)
}
