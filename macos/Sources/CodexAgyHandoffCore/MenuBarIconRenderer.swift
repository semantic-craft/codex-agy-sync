import AppKit
import CoreGraphics
import Foundation

public enum MenuBarIconRenderer {
    public static let pointSize = NSSize(width: 18, height: 18)

    public static func image(
        showAttentionDot: Bool = false,
        language: ResolvedLanguage = .zhHans
    ) -> NSImage {
        let img = NSImage(size: pointSize, flipped: false) { rect in
            guard let ctx = NSGraphicsContext.current?.cgContext else {
                return false
            }
            draw(showAttentionDot: showAttentionDot, in: ctx, bounds: rect)
            return true
        }
        img.isTemplate = true
        img.accessibilityDescription = AppCopy.text(.statusItemLabel, in: language)
        return img
    }

    public static func renderBitmap(
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
            showAttentionDot: showAttentionDot,
            in: ctx,
            bounds: CGRect(origin: .zero, size: pointSize)
        )
        return rep
    }

    private static func draw(
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

        drawForwardProgressRelay(in: ctx, ink: ink)

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

    private static func drawForwardProgressRelay(in ctx: CGContext, ink: CGColor) {
        ctx.setFillColor(ink)
        let seg1 = CGRect(x: 2.0, y: 8.5, width: 1.8, height: 2.0)
        ctx.addPath(CGPath(roundedRect: seg1, cornerWidth: 0.9, cornerHeight: 0.9, transform: nil))
        let seg2 = CGRect(x: 5.1, y: 8.5, width: 3.2, height: 2.0)
        ctx.addPath(CGPath(roundedRect: seg2, cornerWidth: 1.0, cornerHeight: 1.0, transform: nil))
        ctx.fillPath()

        let arrow = CGMutablePath()
        arrow.move(to: CGPoint(x: 9.6, y: 9.5))
        arrow.addLine(to: CGPoint(x: 15.0, y: 9.5))
        arrow.move(to: CGPoint(x: 11.8, y: 13.3))
        arrow.addLine(to: CGPoint(x: 15.6, y: 9.5))
        arrow.addLine(to: CGPoint(x: 11.8, y: 5.7))

        ctx.setStrokeColor(ink)
        ctx.setLineWidth(2.0)
        ctx.setLineCap(.round)
        ctx.setLineJoin(.round)
        ctx.addPath(arrow)
        ctx.strokePath()
    }
}
