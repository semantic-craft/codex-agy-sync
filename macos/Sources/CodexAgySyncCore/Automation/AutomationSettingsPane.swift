import SwiftUI

struct AutomationSettingsPane: View {
    let preferencesStore: PreferencesStore
    let viewModel: AutomationViewModel

    private var lang: ResolvedLanguage {
        preferencesStore.effectiveLanguage
    }

    var body: some View {
        Group {
            if viewModel.showsPreInstallIntro {
                preInstallView
            } else {
                managementView
            }
        }
        .onAppear {
            viewModel.refresh()
        }
        .sheet(
            item: Binding(
                get: { viewModel.pendingDiffReview },
                set: { next in
                    if next == nil {
                        viewModel.dismissDiffReview()
                    }
                }
            )
        ) { review in
            AgentSkillDiffReviewSheet(
                review: review,
                language: lang,
                onCancel: {
                    viewModel.dismissDiffReview()
                },
                onApply: {
                    viewModel.confirmDiffReviewAndApply()
                }
            )
        }
    }

    private var preInstallView: some View {
        VStack(alignment: .leading, spacing: 12) {
            if let notice = viewModel.notice {
                noticeBanner(notice)
            }

            SettingsCard {
                VStack(alignment: .leading, spacing: 12) {
                    HStack(spacing: 10) {
                        ZStack {
                            RoundedRectangle(cornerRadius: 9)
                                .fill(Color.accentColor.opacity(0.14))
                                .frame(width: 34, height: 34)
                            Image(systemName: "terminal")
                                .font(.system(size: 16, weight: .semibold))
                                .foregroundStyle(Color.accentColor)
                        }
                        VStack(alignment: .leading, spacing: 2) {
                            Text(AppCopy.text(.automationIntroTitle, in: lang))
                                .font(.system(size: 14, weight: .semibold))
                            Text(AppCopy.text(.automationIntroSubtitle, in: lang))
                                .font(.system(size: 11))
                                .foregroundStyle(.secondary)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }

                    Divider()

                    VStack(alignment: .leading, spacing: 10) {
                        valueRow(
                            icon: "sparkles",
                            title: AppCopy.text(.automationValueAgentTitle, in: lang),
                            description: AppCopy.text(.automationValueAgentDesc, in: lang)
                        )
                        valueRow(
                            icon: "arrow.triangle.2.circlepath",
                            title: AppCopy.text(.automationValueScriptTitle, in: lang),
                            description: AppCopy.text(.automationValueScriptDesc, in: lang)
                        )
                        valueRow(
                            icon: "curlybraces",
                            title: AppCopy.text(.automationValueJSONTitle, in: lang),
                            description: AppCopy.text(.automationValueJSONDesc, in: lang)
                        )
                    }

                    Text("% codex-agy-sync status --workspace . | jq '.pairing'")
                        .font(.system(size: 11, design: .monospaced))
                        .foregroundStyle(.primary)
                        .padding(.horizontal, 10)
                        .padding(.vertical, 7)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(
                            RoundedRectangle(cornerRadius: 7)
                                .fill(Color.primary.opacity(0.055))
                        )

                    Button {
                        viewModel.installCLIAndSkill()
                    } label: {
                        HStack(spacing: 6) {
                            Image(systemName: "arrow.down.circle.fill")
                                .font(.system(size: 12, weight: .semibold))
                            Text(AppCopy.text(.automationInstallBothCTA, in: lang))
                                .font(.system(size: 12, weight: .semibold))
                        }
                        .foregroundStyle(.white)
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 8)
                        .background(
                            RoundedRectangle(cornerRadius: 8)
                                .fill(Color.accentColor)
                        )
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(FlatSurfaceButtonStyle())
                    .focusEffectDisabled()
                    .accessibilityLabel(AppCopy.text(.automationInstallBothCTA, in: lang))
                    .accessibilityAction {
                        viewModel.installCLIAndSkill()
                    }

                    VStack(alignment: .leading, spacing: 3) {
                        Text(AppCopy.text(.automationInstallPathsFootnote, in: lang))
                            .font(.system(size: 10, weight: .semibold))
                            .foregroundStyle(.secondary)
                        Text("~/.local/bin/codex-agy-sync")
                            .font(.system(size: 10.5, design: .monospaced))
                            .foregroundStyle(.secondary)
                        Text("~/.agents/skills/codex-agy-sync")
                            .font(.system(size: 10.5, design: .monospaced))
                            .foregroundStyle(.secondary)
                        Text("~/.claude/skills/codex-agy-sync")
                            .font(.system(size: 10.5, design: .monospaced))
                            .foregroundStyle(.secondary)
                    }
                }
            }
        }
    }

    private var managementView: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                HStack(spacing: 8) {
                    Text(AppCopy.text(.automationManageTitle, in: lang))
                        .font(.system(size: 13, weight: .semibold))
                    Spacer()
                    SettingsActionButton(
                        AppCopy.text(.automationRefresh, in: lang),
                        systemImage: "arrow.clockwise"
                    ) {
                        viewModel.refresh()
                    }
                }

                if let notice = viewModel.notice {
                    noticeBanner(notice)
                }

                cliCard
                skillCard
            }
            .padding(.bottom, 4)
        }
        .frame(minHeight: 380, maxHeight: 490)
    }

    private var cliCard: some View {
        let state = viewModel.cliState
        return SettingsCard {
            HStack(spacing: 8) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(AppCopy.text(.automationCLICardTitle, in: lang))
                        .font(.system(size: 13, weight: .semibold))
                    Text(AppCopy.text(.automationCLICardSubtitle, in: lang))
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer()
                cliStatusBadge(for: state.status)
            }

            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 6) {
                    Text(AppCopy.text(.automationCLIPathLabel, in: lang))
                        .font(.system(size: 10.5, weight: .semibold))
                        .foregroundStyle(.secondary)
                    Spacer()
                    Button {
                        viewModel.copyCLIPath()
                    } label: {
                        HStack(spacing: 4) {
                            Image(systemName: viewModel.justCopiedPath ? "checkmark" : "doc.on.doc")
                                .font(.system(size: 10, weight: .semibold))
                            Text(
                                AppCopy.text(
                                    viewModel.justCopiedPath ? .automationCopiedPath : .automationCopyPath,
                                    in: lang
                                )
                            )
                            .font(.system(size: 10, weight: .semibold))
                        }
                        .foregroundStyle(viewModel.justCopiedPath ? Color.green : Color.primary)
                        .padding(.horizontal, 7)
                        .padding(.vertical, 3)
                        .background(
                            RoundedRectangle(cornerRadius: 5)
                                .fill(
                                    viewModel.justCopiedPath
                                        ? Color.green.opacity(0.12)
                                        : Color.primary.opacity(0.08)
                                )
                        )
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(FlatSurfaceButtonStyle())
                    .focusEffectDisabled()
                    .accessibilityLabel(AppCopy.text(.automationCopyPath, in: lang))
                    .accessibilityAction {
                        viewModel.copyCLIPath()
                    }
                }

                Text(abbreviatingHome(state.installPath))
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundStyle(.primary)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }

            if let detail = cliDetailText(for: state.status) {
                Text(detail)
                    .font(.system(size: 11))
                    .foregroundStyle(cliDetailColor(for: state.status))
                    .fixedSize(horizontal: false, vertical: true)
            }

            if !state.isInstallDirectoryOnPATH {
                Text(AppCopy.text(.automationCLIPathNotOnPATHHint, in: lang))
                    .font(.system(size: 10.5))
                    .foregroundStyle(.orange)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(8)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(
                        RoundedRectangle(cornerRadius: 7)
                            .fill(Color.orange.opacity(0.1))
                    )
            }

            HStack(spacing: 8) {
                switch state.status {
                case .notInstalled:
                    SettingsPrimaryActionButton(
                        AppCopy.text(.automationCLIInstall, in: lang),
                        systemImage: "arrow.down.circle",
                        disabled: false
                    ) {
                        viewModel.installOrUpdateCLI()
                    }
                case .outdatedSymlink:
                    SettingsPrimaryActionButton(
                        AppCopy.text(.automationCLIUpdate, in: lang),
                        systemImage: "arrow.triangle.2.circlepath",
                        disabled: false
                    ) {
                        viewModel.installOrUpdateCLI()
                    }
                    SettingsActionButton(
                        AppCopy.text(.automationCLIUninstall, in: lang),
                        systemImage: "trash"
                    ) {
                        viewModel.uninstallCLI()
                    }
                case .installed:
                    SettingsActionButton(
                        AppCopy.text(.automationCLIUninstall, in: lang),
                        systemImage: "trash"
                    ) {
                        viewModel.uninstallCLI()
                    }
                case .bundledCLIMissing:
                    SettingsPrimaryActionButton(
                        AppCopy.text(.automationCLIInstall, in: lang),
                        systemImage: "arrow.down.circle",
                        disabled: true
                    ) {}
                case .conflictNonManaged:
                    EmptyView()
                }
                Spacer()
            }
        }
    }

    private var skillCard: some View {
        let state = viewModel.skillState
        return SettingsCard {
            HStack(spacing: 8) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(AppCopy.text(.automationSkillCardTitle, in: lang))
                        .font(.system(size: 13, weight: .semibold))
                    Text(AppCopy.text(.automationSkillCardSubtitle, in: lang))
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer()
                skillStatusBadge(for: state.status)
            }

            VStack(alignment: .leading, spacing: 4) {
                pathReadoutRow(
                    label: AppCopy.text(.automationSkillCanonicalLabel, in: lang),
                    path: abbreviatingHome(state.canonicalDirectoryPath)
                )
                ForEach(state.linkedPaths, id: \.self) { linkPath in
                    pathReadoutRow(
                        label: AppCopy.text(.automationSkillLinkedLabel, in: lang),
                        path: abbreviatingHome(linkPath)
                    )
                }
            }

            HStack(spacing: 6) {
                Image(systemName: "command")
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundStyle(.secondary)
                Text(AppCopy.text(.automationSkillSlashHint, in: lang))
                    .font(.system(size: 10.5))
                    .foregroundStyle(.secondary)
            }

            if let detail = skillDetailText(for: state.status) {
                Text(detail)
                    .font(.system(size: 11))
                    .foregroundStyle(skillDetailColor(for: state.status))
                    .fixedSize(horizontal: false, vertical: true)
            }

            HStack(spacing: 8) {
                switch state.status {
                case .notInstalled:
                    SettingsPrimaryActionButton(
                        AppCopy.text(.automationSkillInstall, in: lang),
                        systemImage: "arrow.down.circle",
                        disabled: false
                    ) {
                        viewModel.installOrReviewSkill()
                    }
                case .installedUpToDate:
                    SettingsActionButton(
                        AppCopy.text(.automationSkillUninstall, in: lang),
                        systemImage: "trash"
                    ) {
                        viewModel.uninstallSkill()
                    }
                case .updateAvailable:
                    SettingsPrimaryActionButton(
                        AppCopy.text(.automationSkillReviewChanges, in: lang),
                        systemImage: "doc.text.magnifyingglass",
                        disabled: false
                    ) {
                        viewModel.installOrReviewSkill()
                    }
                    SettingsActionButton(
                        AppCopy.text(.automationSkillUninstall, in: lang),
                        systemImage: "trash"
                    ) {
                        viewModel.uninstallSkill()
                    }
                case .installedBrokenLinks:
                    SettingsPrimaryActionButton(
                        AppCopy.text(.automationSkillRepairLinks, in: lang),
                        systemImage: "link",
                        disabled: false
                    ) {
                        viewModel.repairSkillLinks()
                    }
                    SettingsActionButton(
                        AppCopy.text(.automationSkillUninstall, in: lang),
                        systemImage: "trash"
                    ) {
                        viewModel.uninstallSkill()
                    }
                case .externallyManaged, .conflictNonManaged:
                    EmptyView()
                }
                Spacer()
            }
        }
    }

    private func valueRow(icon: String, title: String, description: String) -> some View {
        HStack(alignment: .top, spacing: 9) {
            Image(systemName: icon)
                .font(.system(size: 12, weight: .semibold))
                .foregroundStyle(Color.accentColor)
                .frame(width: 18, alignment: .center)
                .padding(.top, 1)
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                    .font(.system(size: 12, weight: .semibold))
                Text(description)
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private func pathReadoutRow(label: String, path: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(label)
                .font(.system(size: 10, weight: .semibold))
                .foregroundStyle(.secondary)
            Text(path)
                .font(.system(size: 11, design: .monospaced))
                .foregroundStyle(.primary)
                .lineLimit(1)
                .truncationMode(.middle)
        }
    }

    private func noticeBanner(_ notice: AutomationNotice) -> some View {
        let tint: Color = switch notice.kind {
        case .info: .accentColor
        case .success: .green
        case .warning: .orange
        case .error: .red
        }
        return HStack(alignment: .top, spacing: 8) {
            Text(notice.message)
                .font(.system(size: 11))
                .foregroundStyle(tint)
                .fixedSize(horizontal: false, vertical: true)
            Spacer()
            SettingsSmallInlineButton(AppCopy.text(.automationDismissNotice, in: lang)) {
                viewModel.dismissNotice()
            }
        }
        .padding(9)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: 8)
                .fill(tint.opacity(0.1))
        )
    }

    private func statusBadge(title: String, color: Color) -> some View {
        HStack(spacing: 5) {
            Circle()
                .fill(color)
                .frame(width: 6, height: 6)
            Text(title)
                .font(.system(size: 10.5, weight: .semibold))
                .foregroundStyle(color)
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 3)
        .background(
            Capsule()
                .fill(color.opacity(0.12))
        )
    }

    private func cliStatusBadge(for status: CLIInstallStatus) -> some View {
        switch status {
        case .installed:
            statusBadge(title: AppCopy.text(.automationCLIStatusInstalled, in: lang), color: .green)
        case .notInstalled:
            statusBadge(title: AppCopy.text(.automationCLIStatusNotInstalled, in: lang), color: .secondary)
        case .outdatedSymlink:
            statusBadge(title: AppCopy.text(.automationCLIStatusOutdated, in: lang), color: .orange)
        case .bundledCLIMissing:
            statusBadge(title: AppCopy.text(.automationCLIStatusBundledMissing, in: lang), color: .orange)
        case .conflictNonManaged:
            statusBadge(title: AppCopy.text(.automationCLIStatusConflict, in: lang), color: .red)
        }
    }

    private func skillStatusBadge(for status: AgentSkillInstallStatus) -> some View {
        switch status {
        case .installedUpToDate:
            statusBadge(title: AppCopy.text(.automationSkillStatusUpToDate, in: lang), color: .green)
        case .notInstalled:
            statusBadge(title: AppCopy.text(.automationSkillStatusNotInstalled, in: lang), color: .secondary)
        case .updateAvailable:
            statusBadge(title: AppCopy.text(.automationSkillStatusUpdateAvailable, in: lang), color: .orange)
        case .installedBrokenLinks:
            statusBadge(title: AppCopy.text(.automationSkillStatusBrokenLinks, in: lang), color: .orange)
        case .externallyManaged:
            statusBadge(title: AppCopy.text(.automationSkillStatusExternal, in: lang), color: .blue)
        case .conflictNonManaged:
            statusBadge(title: AppCopy.text(.automationSkillStatusConflict, in: lang), color: .red)
        }
    }

    private func cliDetailText(for status: CLIInstallStatus) -> String? {
        switch status {
        case .installed, .notInstalled:
            return nil
        case .bundledCLIMissing:
            return AppCopy.text(.automationCLIBundledMissingHint, in: lang)
        case .outdatedSymlink(let currentTarget, let expectedTarget):
            return AppCopy.automationCLIOutdatedDetail(
                currentTarget: abbreviatingHome(currentTarget),
                expectedTarget: abbreviatingHome(expectedTarget),
                in: lang
            )
        case .conflictNonManaged(let existingPath):
            return AppCopy.automationCLIConflictDetail(
                path: abbreviatingHome(existingPath),
                in: lang
            )
        }
    }

    private func cliDetailColor(for status: CLIInstallStatus) -> Color {
        switch status {
        case .conflictNonManaged:
            return .red
        case .outdatedSymlink, .bundledCLIMissing:
            return .orange
        case .installed, .notInstalled:
            return .secondary
        }
    }

    private func skillDetailText(for status: AgentSkillInstallStatus) -> String? {
        switch status {
        case .notInstalled, .installedUpToDate:
            return nil
        case .updateAvailable(let diffs):
            return AppCopy.automationSkillUpdateDetail(fileCount: diffs.count, in: lang)
        case .installedBrokenLinks(let brokenPaths):
            return AppCopy.automationSkillBrokenLinksDetail(count: brokenPaths.count, in: lang)
        case .externallyManaged(let path, let destination):
            return AppCopy.automationSkillExternalDetail(
                path: abbreviatingHome(path),
                destination: abbreviatingHome(destination),
                in: lang
            )
        case .conflictNonManaged(let path):
            return AppCopy.automationSkillConflictDetail(path: abbreviatingHome(path), in: lang)
        }
    }

    private func skillDetailColor(for status: AgentSkillInstallStatus) -> Color {
        switch status {
        case .conflictNonManaged:
            return .red
        case .externallyManaged:
            return .blue
        case .updateAvailable, .installedBrokenLinks:
            return .orange
        case .notInstalled, .installedUpToDate:
            return .secondary
        }
    }

    private func abbreviatingHome(_ path: String) -> String {
        let home = FileManager.default.homeDirectoryForCurrentUser.standardizedFileURL.path
        if path == home {
            return "~"
        }
        if path.hasPrefix(home + "/") {
            return "~" + String(path.dropFirst(home.count))
        }
        return path
    }
}

struct AgentSkillDiffReviewSheet: View {
    let review: AgentSkillDiffReview
    let language: ResolvedLanguage
    let onCancel: () -> Void
    let onApply: () -> Void

    @State private var selectedFileID: String

    init(
        review: AgentSkillDiffReview,
        language: ResolvedLanguage,
        onCancel: @escaping () -> Void,
        onApply: @escaping () -> Void
    ) {
        self.review = review
        self.language = language
        self.onCancel = onCancel
        self.onApply = onApply
        self._selectedFileID = State(initialValue: review.diffs.first?.id ?? "")
    }

    private var activeDiff: AgentSkillFileDiff? {
        review.diffs.first(where: { $0.id == selectedFileID }) ?? review.diffs.first
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            VStack(alignment: .leading, spacing: 4) {
                Text(AppCopy.text(.automationDiffTitle, in: language))
                    .font(.system(size: 15, weight: .semibold))
                Text(AppCopy.text(.automationDiffSubtitle, in: language))
                    .font(.system(size: 11.5))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            if !review.diffs.isEmpty {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 6) {
                        ForEach(review.diffs) { diff in
                            let selected = diff.id == activeDiff?.id
                            Button {
                                selectedFileID = diff.id
                            } label: {
                                HStack(spacing: 5) {
                                    Text(diff.relativePath)
                                        .font(.system(size: 11, weight: .semibold, design: .monospaced))
                                    if diff.existingContent == nil {
                                        Text(AppCopy.text(.automationDiffNewFileBadge, in: language))
                                            .font(.system(size: 9.5, weight: .semibold))
                                            .padding(.horizontal, 4)
                                            .padding(.vertical, 1)
                                            .background(
                                                Capsule()
                                                    .fill(selected ? Color.white.opacity(0.22) : Color.green.opacity(0.16))
                                            )
                                    } else {
                                        Text("+\(diff.addedCount) -\(diff.removedCount)")
                                            .font(.system(size: 10, design: .monospaced))
                                            .opacity(0.8)
                                    }
                                }
                                .foregroundStyle(selected ? Color.white : Color.primary)
                                .padding(.horizontal, 9)
                                .padding(.vertical, 5)
                                .background(
                                    RoundedRectangle(cornerRadius: 7)
                                        .fill(selected ? Color.accentColor : Color.primary.opacity(0.06))
                                )
                                .contentShape(Rectangle())
                            }
                            .buttonStyle(FlatSurfaceButtonStyle())
                            .focusEffectDisabled()
                            .accessibilityLabel(diff.relativePath)
                            .accessibilityAction {
                                selectedFileID = diff.id
                            }
                        }
                    }
                }
            }

            if let diff = activeDiff {
                ScrollView([.vertical, .horizontal]) {
                    LazyVStack(alignment: .leading, spacing: 0) {
                        ForEach(diff.lines) { line in
                            HStack(spacing: 8) {
                                Text(prefixSymbol(for: line.kind))
                                    .font(.system(size: 11, weight: .semibold, design: .monospaced))
                                    .foregroundStyle(foregroundColor(for: line.kind))
                                    .frame(width: 12, alignment: .leading)
                                Text(line.text.isEmpty ? " " : line.text)
                                    .font(.system(size: 11, design: .monospaced))
                                    .foregroundStyle(foregroundColor(for: line.kind))
                                    .textSelection(.enabled)
                                Spacer(minLength: 0)
                            }
                            .padding(.horizontal, 8)
                            .padding(.vertical, 2)
                            .background(backgroundColor(for: line.kind))
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                .frame(minHeight: 260, maxHeight: 340)
                .background(
                    RoundedRectangle(cornerRadius: 8)
                        .fill(Color.primary.opacity(0.03))
                )
                .overlay(
                    RoundedRectangle(cornerRadius: 8)
                        .stroke(Color.primary.opacity(0.1), lineWidth: 0.8)
                )
            }

            HStack(spacing: 8) {
                Spacer()
                SettingsActionButton(
                    AppCopy.text(.automationDiffCancel, in: language),
                    systemImage: "xmark"
                ) {
                    onCancel()
                }
                SettingsPrimaryActionButton(
                    AppCopy.text(.automationDiffApply, in: language),
                    systemImage: "checkmark.circle.fill",
                    disabled: false
                ) {
                    onApply()
                }
            }
        }
        .padding(18)
        .frame(width: 580)
    }

    private func prefixSymbol(for kind: AgentSkillDiffLineKind) -> String {
        switch kind {
        case .context:
            return " "
        case .removed:
            return "-"
        case .added:
            return "+"
        }
    }

    private func foregroundColor(for kind: AgentSkillDiffLineKind) -> Color {
        switch kind {
        case .context:
            return .secondary
        case .removed:
            return .red
        case .added:
            return .green
        }
    }

    private func backgroundColor(for kind: AgentSkillDiffLineKind) -> Color {
        switch kind {
        case .context:
            return .clear
        case .removed:
            return Color.red.opacity(0.1)
        case .added:
            return Color.green.opacity(0.1)
        }
    }
}
