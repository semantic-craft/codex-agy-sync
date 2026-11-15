import AppKit
import SwiftUI

private struct FlatSurfaceButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .opacity(configuration.isPressed ? 0.86 : 1.0)
    }
}

private struct HeroPresentation {
    let statusText: String
    let iconName: String
    let accentColor: Color
    let highContrastCTA: Bool
    let ctaTitle: String

    static func make(for hero: MenubarSessionItem) -> HeroPresentation {
        let targetName = hero.targetDisplayName
        switch hero.state {
        case .interruptedStopped:
            return HeroPresentation(
                statusText: "检测到刚中断的会话（已停止）",
                iconName: "bolt.circle.fill",
                accentColor: .orange,
                highContrastCTA: true,
                ctaTitle: "抢救并切到 \(targetName) 继续"
            )
        case .pendingReconcile:
            return HeroPresentation(
                statusText: "有未完成的挂起交接",
                iconName: "clock.badge.exclamationmark.fill",
                accentColor: .yellow,
                highContrastCTA: true,
                ctaTitle: "核对恢复挂起会话"
            )
        case .completedReady, .alreadyHanded, .activeRunning:
            return HeroPresentation(
                statusText: "最新可接力会话",
                iconName: "arrow.triangle.branch",
                accentColor: .accentColor,
                highContrastCTA: false,
                ctaTitle: "切到 \(targetName) 继续"
            )
        }
    }
}

public struct PopoverView: View {
    @ObservedObject private var viewModel: HandoffViewModel

    public init(viewModel: HandoffViewModel) {
        self.viewModel = viewModel
    }

    public var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            headerBar
            heroSection
            if viewModel.banner != nil || viewModel.cliMissing {
                bannerSection
            }
            recentSection
            Divider()
            footerBar
        }
        .padding(14)
        .frame(width: 360)
        .background(Color(nsColor: .windowBackgroundColor))
    }

    private var headerBar: some View {
        HStack(spacing: 8) {
            Image(systemName: "arrow.left.arrow.right.circle.fill")
                .font(.system(size: 15, weight: .semibold))
                .foregroundStyle(.orange)
            Text("会话接力")
                .font(.system(size: 14, weight: .semibold))

            Spacer()

            Text("⌃⌥H")
                .font(.system(size: 10, weight: .medium, design: .monospaced))
                .foregroundStyle(.secondary)
                .padding(.horizontal, 6)
                .padding(.vertical, 2)
                .background(
                    Capsule()
                        .fill(Color.primary.opacity(0.06))
                )
                .overlay(
                    Capsule()
                        .stroke(Color.primary.opacity(0.12), lineWidth: 0.75)
                )

            Button {
                Task { await viewModel.refresh() }
            } label: {
                HStack(spacing: 4) {
                    if viewModel.isLoading {
                        ProgressView()
                            .controlSize(.small)
                            .scaleEffect(0.75)
                    } else {
                        Image(systemName: "arrow.clockwise")
                            .font(.system(size: 11, weight: .medium))
                    }
                    Text("刷新")
                        .font(.system(size: 11, weight: .medium))
                }
                .foregroundStyle(.primary)
                .padding(.horizontal, 8)
                .padding(.vertical, 4)
                .background(
                    RoundedRectangle(cornerRadius: 6)
                        .fill(Color.primary.opacity(0.07))
                )
                .contentShape(Rectangle())
            }
            .buttonStyle(FlatSurfaceButtonStyle())
            .focusEffectDisabled()
            .disabled(viewModel.isLoading)
            .accessibilityLabel("刷新会话列表")
        }
    }

    @ViewBuilder
    private var heroSection: some View {
        if let hero = viewModel.snapshot?.hero {
            let spec = HeroPresentation.make(for: hero)
            let isBusy = viewModel.busyItemId == hero.id || (hero.state == .pendingReconcile && viewModel.busyItemId == hero.workspacePath)

            VStack(alignment: .leading, spacing: 9) {
                HStack(spacing: 6) {
                    Image(systemName: spec.iconName)
                        .font(.system(size: 12, weight: .semibold))
                        .foregroundStyle(spec.accentColor)
                    Text(spec.statusText)
                        .font(.system(size: 11, weight: .semibold))
                        .foregroundStyle(spec.highContrastCTA ? spec.accentColor : .secondary)
                    Spacer()
                    Text(hero.directionBadge)
                        .font(.system(size: 10, weight: .semibold, design: .monospaced))
                        .foregroundStyle(.primary)
                        .padding(.horizontal, 6)
                        .padding(.vertical, 2)
                        .background(
                            Capsule()
                                .fill(Color.primary.opacity(0.07))
                        )
                }

                Text(hero.title)
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(.primary)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)

                Text("\(hero.workspaceName) · \(hero.turnCount) 轮")
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)

                if let errText = hero.pendingError, !errText.isEmpty, hero.state == .pendingReconcile {
                    Text(errText)
                        .font(.system(size: 10))
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                }

                Button {
                    triggerHeroAction(hero)
                } label: {
                    HStack(spacing: 6) {
                        if isBusy {
                            ProgressView()
                                .controlSize(.small)
                                .tint(spec.highContrastCTA ? .white : .primary)
                        }
                        Text(spec.ctaTitle)
                            .font(.system(size: 12, weight: .semibold))
                    }
                    .foregroundStyle(spec.highContrastCTA ? Color.white : Color.primary)
                    .frame(maxWidth: .infinity, minHeight: 34)
                    .background(
                        RoundedRectangle(cornerRadius: 8)
                            .fill(
                                spec.highContrastCTA
                                    ? (hero.state == .interruptedStopped ? Color.orange : Color.amberFill)
                                    : Color.primary.opacity(0.09)
                            )
                    )
                    .contentShape(Rectangle())
                }
                .buttonStyle(FlatSurfaceButtonStyle())
                .focusEffectDisabled()
                .disabled(viewModel.busyItemId != nil)
                .accessibilityLabel(spec.ctaTitle)
                .accessibilityAction {
                    triggerHeroAction(hero)
                }
            }
            .padding(12)
            .background(
                RoundedRectangle(cornerRadius: 12)
                    .fill(
                        hero.state == .interruptedStopped
                            ? Color.orange.opacity(0.11)
                            : (hero.state == .pendingReconcile ? Color.yellow.opacity(0.11) : Color.primary.opacity(0.04))
                    )
            )
            .overlay(
                RoundedRectangle(cornerRadius: 12)
                    .stroke(
                        hero.state == .interruptedStopped
                            ? Color.orange.opacity(0.38)
                            : (hero.state == .pendingReconcile ? Color.yellow.opacity(0.42) : Color.primary.opacity(0.12)),
                        lineWidth: 1
                    )
            )
        } else {
            VStack(alignment: .leading, spacing: 4) {
                Text("当前无待抢救会话")
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(.primary)
                Text("下方列表可直接接力已完成或运行前快照")
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(12)
            .background(
                RoundedRectangle(cornerRadius: 12)
                    .fill(Color.primary.opacity(0.04))
            )
            .overlay(
                RoundedRectangle(cornerRadius: 12)
                    .stroke(Color.primary.opacity(0.1), lineWidth: 1)
            )
        }
    }

    private var bannerSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let banner = viewModel.banner {
                let isSuccess: Bool = {
                    if case .success = banner { return true }
                    return false
                }()
                let text: String = {
                    switch banner {
                    case .success(let msg), .error(let msg):
                        return msg
                    }
                }()

                HStack(alignment: .top, spacing: 8) {
                    Image(systemName: isSuccess ? "checkmark.circle.fill" : "exclamationmark.triangle.fill")
                        .font(.system(size: 12))
                        .foregroundStyle(isSuccess ? .green : .red)
                        .padding(.top, 1)

                    Text(text)
                        .font(.system(size: 11))
                        .foregroundStyle(.primary)
                        .fixedSize(horizontal: false, vertical: true)
                        .frame(maxWidth: .infinity, alignment: .leading)

                    Button {
                        viewModel.dismissBanner()
                    } label: {
                        Image(systemName: "xmark")
                            .font(.system(size: 10, weight: .medium))
                            .foregroundStyle(.secondary)
                            .padding(3)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(FlatSurfaceButtonStyle())
                    .focusEffectDisabled()
                    .accessibilityLabel("关闭提示：\(text)")
                }
                .padding(9)
                .background(
                    RoundedRectangle(cornerRadius: 8)
                        .fill((isSuccess ? Color.green : Color.red).opacity(0.1))
                )
                .overlay(
                    RoundedRectangle(cornerRadius: 8)
                        .stroke((isSuccess ? Color.green : Color.red).opacity(0.3), lineWidth: 0.75)
                )
            }

            if viewModel.cliMissing {
                Button {
                    chooseCLIExecutable()
                } label: {
                    Text("选择 CLI…")
                        .font(.system(size: 11, weight: .semibold))
                        .foregroundStyle(.primary)
                        .padding(.horizontal, 10)
                        .padding(.vertical, 5)
                        .background(
                            RoundedRectangle(cornerRadius: 6)
                                .fill(Color.primary.opacity(0.08))
                        )
                        .overlay(
                            RoundedRectangle(cornerRadius: 6)
                                .stroke(Color.primary.opacity(0.15), lineWidth: 0.75)
                        )
                        .contentShape(Rectangle())
                }
                .buttonStyle(FlatSurfaceButtonStyle())
                .focusEffectDisabled()
                .accessibilityLabel("选择 CLI…")
            }
        }
    }

    private var recentSection: some View {
        let items = Array((viewModel.snapshot?.recent ?? []).prefix(4))
        return VStack(alignment: .leading, spacing: 7) {
            Text("最近会话")
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(.secondary)

            if items.isEmpty {
                Text("暂无最近会话")
                    .font(.system(size: 11))
                    .foregroundStyle(.tertiary)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.vertical, 6)
            } else {
                VStack(spacing: 6) {
                    ForEach(items) { item in
                        recentRow(item)
                    }
                }
            }
        }
    }

    private func recentRow(_ item: MenubarSessionItem) -> some View {
        let pillColor: Color = item.host == "codex" ? .blue : .purple
        let isBusy = viewModel.busyItemId == item.id

        return HStack(spacing: 8) {
            Text(item.sourceShortTag)
                .font(.system(size: 9, weight: .bold, design: .monospaced))
                .foregroundStyle(pillColor)
                .padding(.horizontal, 5)
                .padding(.vertical, 3)
                .background(
                    RoundedRectangle(cornerRadius: 5)
                        .fill(pillColor.opacity(0.12))
                )

            VStack(alignment: .leading, spacing: 2) {
                Text(item.title)
                    .font(.system(size: 12, weight: .medium))
                    .foregroundStyle(.primary)
                    .lineLimit(1)
                    .truncationMode(.tail)

                Text("\(item.workspaceName) · \(item.turnCount) 轮")
                    .font(.system(size: 10))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
            .frame(maxWidth: .infinity, alignment: .leading)

            trailingControl(for: item, isBusy: isBusy)
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 6)
        .background(
            RoundedRectangle(cornerRadius: 8)
                .fill(Color.primary.opacity(0.035))
        )
    }

    @ViewBuilder
    private func trailingControl(for item: MenubarSessionItem, isBusy: Bool) -> some View {
        switch item.state {
        case .interruptedStopped, .completedReady:
            Button {
                Task { await viewModel.handoff(item) }
            } label: {
                HStack(spacing: 4) {
                    if isBusy {
                        ProgressView()
                            .controlSize(.small)
                            .scaleEffect(0.7)
                    }
                    Text("接力")
                        .font(.system(size: 11, weight: .semibold))
                }
                .foregroundStyle(item.state == .interruptedStopped ? .white : .primary)
                .padding(.horizontal, 9)
                .padding(.vertical, 4)
                .background(
                    RoundedRectangle(cornerRadius: 6)
                        .fill(item.state == .interruptedStopped ? Color.orange : Color.primary.opacity(0.09))
                )
                .contentShape(Rectangle())
            }
            .buttonStyle(FlatSurfaceButtonStyle())
            .focusEffectDisabled()
            .disabled(viewModel.busyItemId != nil)
            .accessibilityLabel("接力 \(item.title)")

        case .pendingReconcile:
            Button {
                Task { await viewModel.reconcile(workspacePath: item.workspacePath) }
            } label: {
                Text("核对")
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(.primary)
                    .padding(.horizontal, 9)
                    .padding(.vertical, 4)
                    .background(
                        RoundedRectangle(cornerRadius: 6)
                            .fill(Color.yellow.opacity(0.24))
                    )
                    .contentShape(Rectangle())
            }
            .buttonStyle(FlatSurfaceButtonStyle())
            .focusEffectDisabled()
            .disabled(viewModel.busyItemId != nil)
            .accessibilityLabel("核对 \(item.title)")

        case .activeRunning:
            mutedStatusTag("运行中")

        case .alreadyHanded:
            mutedStatusTag("已交接")
        }
    }

    private func mutedStatusTag(_ label: String) -> some View {
        Text(label)
            .font(.system(size: 10, weight: .medium))
            .foregroundStyle(.secondary)
            .padding(.horizontal, 7)
            .padding(.vertical, 3)
            .background(
                Capsule()
                    .fill(Color.primary.opacity(0.06))
            )
    }

    private var footerBar: some View {
        HStack(spacing: 8) {
            daemonDot(label: "AGY", active: viewModel.snapshot?.agyDaemonActive ?? false)
            daemonDot(label: "Codex", active: viewModel.snapshot?.codexDaemonActive ?? false)

            Spacer()

            Text("自动配对 · 含中断抢救")
                .font(.system(size: 10))
                .foregroundStyle(.tertiary)
                .lineLimit(1)

            Button {
                NSApp.terminate(nil)
            } label: {
                Text("退出")
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 7)
                    .padding(.vertical, 3)
                    .background(
                        RoundedRectangle(cornerRadius: 5)
                            .fill(Color.primary.opacity(0.06))
                    )
                    .contentShape(Rectangle())
            }
            .buttonStyle(FlatSurfaceButtonStyle())
            .focusEffectDisabled()
            .accessibilityLabel("退出应用")
        }
    }

    private func daemonDot(label: String, active: Bool) -> some View {
        HStack(spacing: 4) {
            Circle()
                .fill(active ? Color.green : Color.gray.opacity(0.45))
                .frame(width: 6, height: 6)
            Text(label)
                .font(.system(size: 10, weight: .medium))
                .foregroundStyle(.secondary)
        }
    }

    private func triggerHeroAction(_ hero: MenubarSessionItem) {
        Task {
            if hero.state == .pendingReconcile {
                await viewModel.reconcile(workspacePath: hero.workspacePath)
            } else {
                await viewModel.handoff(hero)
            }
        }
    }

    private func chooseCLIExecutable() {
        let panel = NSOpenPanel()
        panel.title = "选择 codex-agy-sync 可执行文件"
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        NSApp.activate(ignoringOtherApps: true)
        if panel.runModal() == .OK, let url = panel.url {
            viewModel.setCustomCLIPath(url.path)
        }
    }
}

private extension Color {
    static var amberFill: Color {
        Color(red: 0.78, green: 0.50, blue: 0.05)
    }
}
