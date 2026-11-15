import AppKit
import CoreGraphics
import Foundation

public enum MenuBarIconRenderer {
    public static let pointSize = NSSize(width: 18, height: 18)

    public static func image(
        for style: MenuBarIconStyle,
        showAttentionDot: Bool = false
    ) -> NSImage {
        let img = NSImage(size: pointSize, flipped: false) { rect in
            guard let ctx = NSGraphicsContext.current?.cgContext else {
                return false
            }
            draw(style: style, showAttentionDot: showAttentionDot, in: ctx, bounds: rect)
            return true
        }
        img.isTemplate = true
        img.accessibilityDescription = style.accessibilityDescription
        return img
    }

    public static func renderBitmap(
        for style: MenuBarIconStyle,
        showAttentionDot: Bool = false,
        pixelSize: Int = 36
    ) -> NSBitmapImageRep? {
        guard pixelSize > 0,
              let rep = NSBitmapImageRep(
                  bitmapDataPlanes: nil,
                  pixelsWide: pixelSize,
                  pixelsHigh: pixelSize,
                  bitsPerSample: 8,
                  samplesPerPixel: 4,
                  hasAlpha: true,
                  isPlanar: false,
                  colorSpaceName: .deviceRGB,
                  bitmapFormat: [],
                  bytesPerRow: 0,
                  bitsPerPixel: 0
              )
        else {
            return nil
        }
        rep.size = pointSize

        guard let nsCtx = NSGraphicsContext(bitmapImageRep: rep) else {
            return nil
        }
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = nsCtx
        defer { NSGraphicsContext.restoreGraphicsState() }

        let ctx = nsCtx.cgContext
        ctx.clear(CGRect(origin: .zero, size: pointSize))
        draw(
            style: style,
            showAttentionDot: showAttentionDot,
            in: ctx,
            bounds: CGRect(origin: .zero, size: pointSize)
        )
        return rep
    }

    private static func draw(
        style: MenuBarIconStyle,
        showAttentionDot: Bool,
        in ctx: CGContext,
        bounds: CGRect
    ) {
        ctx.saveGState()
        defer { ctx.restoreGState() }

        ctx.setAllowsAntialiasing(true)
        ctx.setShouldAntialias(true)
        ctx.beginTransparencyLayer(in: bounds, auxiliaryInfo: nil)
        defer { ctx.endTransparencyLayer() }

        let ink = CGColor(gray: 0.0, alpha: 1.0)
        ctx.setFillColor(ink)
        ctx.setStrokeColor(ink)

        switch style {
        case .relayBridge:
            drawRelayBridge(in: ctx, ink: ink)
        case .pulseBolt:
            drawPulseBolt(in: ctx, ink: ink)
        case .twinArrows:
            drawTwinArrows(in: ctx, ink: ink)
        case .syncOrbit:
            drawSyncOrbit(in: ctx, ink: ink)
        }

        if showAttentionDot {
            let dotCenter = CGPoint(x: 14.6, y: 3.4)
            let haloRadius: CGFloat = 3.2
            let dotRadius: CGFloat = 2.2

            ctx.saveGState()
            ctx.setBlendMode(.clear)
            ctx.fillEllipse(
                in: CGRect(
                    x: dotCenter.x - haloRadius,
                    y: dotCenter.y - haloRadius,
                    width: haloRadius * 2,
                    height: haloRadius * 2
                )
            )
            ctx.restoreGState()

            ctx.setFillColor(ink)
            ctx.fillEllipse(
                in: CGRect(
                    x: dotCenter.x - dotRadius,
                    y: dotCenter.y - dotRadius,
                    width: dotRadius * 2,
                    height: dotRadius * 2
                )
            )
        }
    }

    private static func drawRelayBridge(in ctx: CGContext, ink: CGColor) {
        let railWidth: CGFloat = 2.2
        let leftRect = CGRect(x: 4.2 - railWidth / 2, y: 2.6, width: railWidth, height: 12.8)
        let rightRect = CGRect(x: 13.8 - railWidth / 2, y: 2.6, width: railWidth, height: 12.8)

        ctx.setFillColor(ink)
        ctx.addPath(CGPath(roundedRect: leftRect, cornerWidth: railWidth / 2, cornerHeight: railWidth / 2, transform: nil))
        ctx.addPath(CGPath(roundedRect: rightRect, cornerWidth: railWidth / 2, cornerHeight: railWidth / 2, transform: nil))
        ctx.fillPath()

        let start = CGPoint(x: 4.2, y: 5.6)
        let end = CGPoint(x: 13.8, y: 12.4)
        let curve = CGMutablePath()
        curve.move(to: start)
        curve.addCurve(to: end, control1: CGPoint(x: 8.8, y: 5.6), control2: CGPoint(x: 9.2, y: 12.4))

        ctx.setStrokeColor(ink)
        ctx.setLineWidth(1.85)
        ctx.setLineCap(.round)
        ctx.addPath(curve)
        ctx.strokePath()

        let nodeRadius: CGFloat = 2.05
        for node in [start, end] {
            ctx.fillEllipse(
                in: CGRect(
                    x: node.x - nodeRadius,
                    y: node.y - nodeRadius,
                    width: nodeRadius * 2,
                    height: nodeRadius * 2
                )
            )
        }
    }

    private static func drawPulseBolt(in ctx: CGContext, ink: CGColor) {
        let brackets = CGMutablePath()
        brackets.move(to: CGPoint(x: 4.6, y: 14.2))
        brackets.addLine(to: CGPoint(x: 2.6, y: 14.2))
        brackets.addLine(to: CGPoint(x: 2.6, y: 3.8))
        brackets.addLine(to: CGPoint(x: 4.6, y: 3.8))

        brackets.move(to: CGPoint(x: 13.4, y: 14.2))
        brackets.addLine(to: CGPoint(x: 15.4, y: 14.2))
        brackets.addLine(to: CGPoint(x: 15.4, y: 3.8))
        brackets.addLine(to: CGPoint(x: 13.4, y: 3.8))

        ctx.setStrokeColor(ink)
        ctx.setLineWidth(1.6)
        ctx.setLineCap(.round)
        ctx.setLineJoin(.round)
        ctx.addPath(brackets)
        ctx.strokePath()

        let bolt = CGMutablePath()
        bolt.move(to: CGPoint(x: 10.3, y: 15.5))
        bolt.addLine(to: CGPoint(x: 5.5, y: 9.2))
        bolt.addLine(to: CGPoint(x: 8.7, y: 9.2))
        bolt.addLine(to: CGPoint(x: 7.7, y: 2.5))
        bolt.addLine(to: CGPoint(x: 12.5, y: 8.8))
        bolt.addLine(to: CGPoint(x: 9.3, y: 8.8))
        bolt.closeSubpath()

        ctx.setFillColor(ink)
        ctx.addPath(bolt)
        ctx.fillPath()
    }

    private static func drawTwinArrows(in ctx: CGContext, ink: CGColor) {
        let arrows = CGMutablePath()
        arrows.move(to: CGPoint(x: 3.0, y: 12.0))
        arrows.addLine(to: CGPoint(x: 14.4, y: 12.0))
        arrows.move(to: CGPoint(x: 11.1, y: 14.7))
        arrows.addLine(to: CGPoint(x: 14.6, y: 12.0))
        arrows.addLine(to: CGPoint(x: 11.1, y: 9.3))

        arrows.move(to: CGPoint(x: 15.0, y: 6.0))
        arrows.addLine(to: CGPoint(x: 3.6, y: 6.0))
        arrows.move(to: CGPoint(x: 6.9, y: 8.7))
        arrows.addLine(to: CGPoint(x: 3.4, y: 6.0))
        arrows.addLine(to: CGPoint(x: 6.9, y: 3.3))

        ctx.setStrokeColor(ink)
        ctx.setLineWidth(1.85)
        ctx.setLineCap(.round)
        ctx.setLineJoin(.round)
        ctx.addPath(arrows)
        ctx.strokePath()
    }

    private static func drawSyncOrbit(in ctx: CGContext, ink: CGColor) {
        let leftCenter = CGPoint(x: 6.8, y: 9.0)
        let rightCenter = CGPoint(x: 11.2, y: 9.0)
        let radius: CGFloat = 4.3
        let strokeWidth: CGFloat = 1.8

        ctx.setStrokeColor(ink)
        ctx.setLineWidth(strokeWidth)

        ctx.strokeEllipse(
            in: CGRect(
                x: leftCenter.x - radius,
                y: leftCenter.y - radius,
                width: radius * 2,
                height: radius * 2
            )
        )

        ctx.saveGState()
        ctx.setBlendMode(.clear)
        ctx.setLineWidth(strokeWidth + 1.5)
        let clearRightUpper = CGMutablePath()
        clearRightUpper.addArc(
            center: rightCenter,
            radius: radius,
            startAngle: .pi * 0.62,
            endAngle: .pi * 0.92,
            clockwise: false
        )
        ctx.addPath(clearRightUpper)
        ctx.strokePath()
        ctx.restoreGState()

        ctx.setStrokeColor(ink)
        ctx.setLineWidth(strokeWidth)
        ctx.strokeEllipse(
            in: CGRect(
                x: rightCenter.x - radius,
                y: rightCenter.y - radius,
                width: radius * 2,
                height: radius * 2
            )
        )

        ctx.saveGState()
        ctx.setBlendMode(.clear)
        ctx.setLineWidth(strokeWidth + 1.5)
        let clearLeftLower = CGMutablePath()
        clearLeftLower.addArc(
            center: leftCenter,
            radius: radius,
            startAngle: -.pi * 0.38,
            endAngle: -.pi * 0.08,
            clockwise: false
        )
        ctx.addPath(clearLeftLower)
        ctx.strokePath()
        ctx.restoreGState()

        ctx.setStrokeColor(ink)
        ctx.setLineWidth(strokeWidth)
        let overLeftLower = CGMutablePath()
        overLeftLower.addArc(
            center: leftCenter,
            radius: radius,
            startAngle: -.pi * 0.42,
            endAngle: -.pi * 0.04,
            clockwise: false
        )
        ctx.addPath(overLeftLower)
        ctx.strokePath()
    }
}
