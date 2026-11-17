import Foundation
import Observation

@MainActor
public final class ObservationToken {
    private var isCancelled = false

    public init() {}

    fileprivate var cancelled: Bool { isCancelled }

    public func cancel() {
        isCancelled = true
    }
}

@MainActor
private final class ObservationDriver<Value: Equatable & Sendable> {
    let token = ObservationToken()
    private let read: @MainActor () -> Value
    private let onChange: @MainActor (Value) -> Void
    private var lastValue: Value

    init(
        read: @escaping @MainActor () -> Value,
        onChange: @escaping @MainActor (Value) -> Void
    ) {
        self.read = read
        self.onChange = onChange
        self.lastValue = read()
    }

    func arm() {
        guard !token.cancelled else { return }
        withObservationTracking {
            _ = read()
        } onChange: {
            Task { @MainActor [self] in
                self.handleChange()
            }
        }
    }

    private func handleChange() {
        guard !token.cancelled else { return }
        let next = read()
        if next != lastValue {
            lastValue = next
            onChange(next)
        }
        arm()
    }
}

@MainActor
public func observeContinuously<Value: Equatable & Sendable>(
    _ read: @escaping @MainActor () -> Value,
    onChange: @escaping @MainActor (Value) -> Void
) -> ObservationToken {
    let driver = ObservationDriver(read: read, onChange: onChange)
    driver.arm()
    return driver.token
}
