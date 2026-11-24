import CoreGraphics
import Foundation

public enum WindowGeometry {
    public static let defaultMinimumSize = CGSize(width: 120, height: 80)
    public static let defaultSplitDeadzone: CGFloat = 36

    public static func nearestCorner(for pointer: CGPoint, in frame: CGRect) -> ResizeAnchorCorner {
        let isLeft = pointer.x < frame.midX
        let isTop = pointer.y < frame.midY
        switch (isLeft, isTop) {
        case (true, true):
            return .topLeft
        case (false, true):
            return .topRight
        case (true, false):
            return .bottomLeft
        case (false, false):
            return .bottomRight
        }
    }

    public static func movedOrigin(
        initialOrigin: CGPoint,
        startPointer: CGPoint,
        currentPointer: CGPoint
    ) -> CGPoint {
        CGPoint(
            x: initialOrigin.x + (currentPointer.x - startPointer.x),
            y: initialOrigin.y + (currentPointer.y - startPointer.y)
        )
    }

    public static func resizedFrame(
        initialFrame: CGRect,
        corner: ResizeAnchorCorner,
        startPointer: CGPoint,
        currentPointer: CGPoint,
        minimumSize: CGSize = defaultMinimumSize
    ) -> CGRect {
        let dx = currentPointer.x - startPointer.x
        let dy = currentPointer.y - startPointer.y
        let minWidth = max(1, minimumSize.width)
        let minHeight = max(1, minimumSize.height)

        switch corner {
        case .bottomRight:
            let width = max(minWidth, initialFrame.width + dx)
            let height = max(minHeight, initialFrame.height + dy)
            return CGRect(
                x: initialFrame.minX,
                y: initialFrame.minY,
                width: width,
                height: height
            )
        case .bottomLeft:
            let width = max(minWidth, initialFrame.width - dx)
            let height = max(minHeight, initialFrame.height + dy)
            return CGRect(
                x: initialFrame.maxX - width,
                y: initialFrame.minY,
                width: width,
                height: height
            )
        case .topRight:
            let width = max(minWidth, initialFrame.width + dx)
            let height = max(minHeight, initialFrame.height - dy)
            return CGRect(
                x: initialFrame.minX,
                y: initialFrame.maxY - height,
                width: width,
                height: height
            )
        case .topLeft:
            let width = max(minWidth, initialFrame.width - dx)
            let height = max(minHeight, initialFrame.height - dy)
            return CGRect(
                x: initialFrame.maxX - width,
                y: initialFrame.maxY - height,
                width: width,
                height: height
            )
        }
    }

    public static func splitGestureAction(
        startPointer: CGPoint,
        currentPointer: CGPoint,
        deadzone: CGFloat = defaultSplitDeadzone
    ) -> SplitScreenGestureAction? {
        let dx = currentPointer.x - startPointer.x
        let dy = currentPointer.y - startPointer.y
        let absX = abs(dx)
        let absY = abs(dy)
        let threshold = max(0, deadzone)
        guard max(absX, absY) >= threshold else {
            return nil
        }
        if absX >= absY {
            return dx < 0 ? .splitLeft : .splitRight
        }
        return dy < 0 ? .enterFullScreen : .exitFullScreenOrSplit
    }

    public static func adjustOriginAfterActualSize(
        desiredFrame: CGRect,
        actualSize: CGSize,
        corner: ResizeAnchorCorner
    ) -> CGPoint {
        let originX: CGFloat
        switch corner {
        case .topLeft, .bottomLeft:
            originX = desiredFrame.maxX - actualSize.width
        case .topRight, .bottomRight:
            originX = desiredFrame.minX
        }

        let originY: CGFloat
        switch corner {
        case .topLeft, .topRight:
            originY = desiredFrame.maxY - actualSize.height
        case .bottomLeft, .bottomRight:
            originY = desiredFrame.minY
        }

        return CGPoint(x: originX, y: originY)
    }
}
