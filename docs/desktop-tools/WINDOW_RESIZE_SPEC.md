# WINDOW_RESIZE_SPEC.md（提示词 1：窗口调整功能规格）

日期：2026-11-24
前置依赖：[INTEGRATION.md](./INTEGRATION.md)
范围约束：仅设计并实现“共享桌面输入接缝（`DesktopInputHub`）+ 窗口调整”，不夹带 Clean Paste 或 App Hotkeys 的功能逻辑。本模块默认关闭，用户显式开启后生效。

---

## 1. 行为规格（严格区分三类标注）

### 1.1 截图事实（验收基准）

1. **指针下窗口、不必先点击**：移动或调整尺寸时，操作目标为当前鼠标指针所在的窗口，不要求先点击该窗口使其成为前台活跃窗口。
2. **移动**：按住 `fn + ctrl` 并拖动，移动指针下窗口。
3. **调整尺寸与最近角开关**：在移动修饰键基础上再按住 `option`（即 `fn + ctrl + option`）并拖动以调整该窗口尺寸；提供 `nearest corner`（最近角）开关，开启后从距离指针最近的窗口角拉动调整尺寸。
4. **键盘摆放（作用于当前活跃窗口）**：`fn + ctrl + 方向键 / F / C / R` 作用于当前前台活跃窗口（非指针下窗口），分别对应：
   - 方向键（`← / → / ↑ / ↓`）：系统平铺
   - `F`：填充（Fill）
   - `C`：居中（Center）
   - `R`：还原（Return to Previous Size）
5. **分屏与全屏手势（松手后应用）**：按住 `fn + ctrl + option + cmd` 并拖动：
   - 向左 / 向右：进入 Split View（左/右分屏），进入分屏后由系统让用户选择另一侧窗口，或按 `Esc` 取消。
   - 向上：进入全屏。
   - 向下：退出全屏；若当前处于分屏状态，分屏两侧窗口均退出。
   - 松开按键或结束拖动后才真正应用，拖动过程中可取消。
6. **排除 App**：处于排除名单中的应用中，上述按键与手势应完整透传，不由本功能吞掉。

### 1.2 平台设计建议（经 Apple 官方文档与开源参考比选后拟定）

- **修饰键与事件捕获**：采用 `DesktopInputHub` 下的 `SharedEventTapRouter`（单实例 `.cgSessionEventTap`）。因为 Carbon `RegisterEventHotKey` 不支持 `fn`（`kCGEventFlagMaskSecondaryFn`），移动/缩放/分屏手势及 `fn + ctrl` 键盘摆放统一经 `SharedEventTapRouter` 处理，现有 `⌃⌥H` 同步热键继续留在 `CarbonHotKeyRegistry`。
- **指针下窗口解析（不激活）**：通过 `AXUIElementCopyElementAtPosition(systemWide, cgPoint.x, cgPoint.y, &element)` 向上回溯 `kAXWindowAttribute` / `kAXParentAttribute` 找到 `AXWindow`；若命中失败或返回应用级容器，回退用 `CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID)` 按顶层窗口列表定位 PID 与窗口 frame，再匹配该 PID 的 `kAXWindowsAttribute`。全程不调用 `activate()` 或 `AXRaise`。
- **Electron/Chromium 平滑拖动优化**：在拖动/缩放会话开始时读取目标应用的 `AXEnhancedUserInterface` 属性；若为 `true`，会话期间临时置 `false`，结束或取消时立即恢复原值，避免 Chrome / VS Code / Antigravity / Codex Desktop 拖动卡顿或位置漂移。
- **最近角算法**：关闭 `nearest corner` 时固定以右下角缩放（锚定左上角）；开启 `nearest corner` 时在会话开始瞬间将窗口按中心线分为 4 个象限（左上、右上、左下、右下），锁定距离初始指针最近的角，拖动时保持对角固定并同步更新 `kAXPositionAttribute` 与 `kAXSizeAttribute`，同时按窗口最小尺寸（或读取不到时下限 `120x80pt`）钳制防止反转越界。
- **键盘摆放调用真实系统原生平铺**：在 macOS 15+（含本机 `macOS 27.2.1`），先查当前前台应用的 `AXMenuBar` -> `Window` 菜单 -> `Move & Resize` 子菜单对应 `AXMenuItem`（按菜单快捷键属性 `kAXMenuItemCmdVirtualKeyAttribute`/`kAXMenuItemCmdCharAttribute` + 修饰位优先匹配，并以中英文系统标题为辅助），执行 `AXUIElementPerformAction(item, kAXPressAction)` 触发**真正的系统原生平铺**。绝不以自算 frame 冒充系统原生平铺。
- **分屏与全屏手势调用真实系统原生入口**：
  - 拖动过程显示非激活半透明方向指示悬浮层（`NSPanel` 设 `.nonactivatingPanel`，`ignoresMouseEvents = true`，不抢焦点），超过死区阈值（默认 `36pt`）后高亮目标动作，退回死区内或按 `Esc` 立即取消。
  - **进入全屏（上）**：优先 `AXUIElementSetAttributeValue(window, "AXFullScreen", true)`，失败回退 `AXPress` 窗口的 `AXFullScreenButton`。
  - **退出全屏 / 双侧退出分屏（下）**：对当前窗口设置 `AXFullScreen = false`；同时扫描同一显示器可见区域内的其他在屏窗口，若发现另一窗口也满足 `AXFullScreen == true`（分屏配对侧），同步对其设置 `AXFullScreen = false`。
  - **进入左/右 Split View（左/右）**：通过目标窗口的 `AXFullScreenButton` 执行 `AXShowMenu`（或查前台 `AXMenuBar` -> `Window` -> `Full Screen Tile` / `Tile Window to Left/Right of Screen`）并执行对应 `AXMenuItem` 的 `AXPress`，由 macOS 原生进入分屏配对选择界面（原生支持点击另一侧窗口或按 `Esc` 取消）。若目标窗口不支持原生分屏，诚实中止并记录，绝不以 50% resize 冒充。

### 1.3 待验证项（落地前或实机首测中必须核实，绝不写成既成事实）

1. **“拖动”的物理交互方式**：截图写“按住 `fn + ctrl` 并拖动”。在 macOS 同类工具中存在两种模式：
   - 模式 A（无点击移动，如 SwiftShift）：只需按住修饰键并移动光标即可拖动，松开修饰键结束。
   - 模式 B（修饰键 + 左键拖动，如 Easy Move+Resize）：按住修饰键同时按下鼠标左键拖动（吞掉左键按下与拖动事件以防激活窗口）。
   - 当前在未观察 Kanary 实机交互前标记为**待验证**；实现架构上通过状态机同时支持两种触发源，默认交互模式需在实现前与用户确认（或在实机只读观察 Kanary 可见交互一次核定）。
2. **非活跃窗口直接进入 Split View 时是否必须先激活**：`AXFullScreenButton` 的 `AXShowMenu` 与 `Window` 菜单栏对后台窗口在 `macOS 27.2.1` 上是否要求先 `AXRaise`/激活，需在实机验收确认。
3. **macOS 15+ / 27.2.1 原生 `Globe + Ctrl + 方向键/F/C/R` 与本模块拦截关系**：系统自身已内置这套快捷键用于 `Window -> Move & Resize`。当本模块启用且当前应用未被排除时，直接调用 `AXMenuItem` 与透传给系统自身在 Electron/第三方应用上的表现差异需在实机实测；当应用被加入排除名单时，本模块确保透传不吞键（若用户希望连系统自带平铺也不触发，属于系统设置范畴，需如实说明）。
4. **多显示器与 Stage Manager 边界**：跨不同缩放比（Retina / 非 Retina）显示器的 Quartz 坐标系换算、屏幕顶部菜单栏与 Dock 钳制，以及 Stage Manager 开启时 Split View 菜单项是否被系统隐藏，需实机验收记录。
5. **不可移动 / 不可调整窗口**：固定尺寸对话框、`AXSheet`、`AXSystemDialog`、桌面的 Finder `AXScrollArea`、状态栏悬浮窗等，必须经 `AXUIElementIsAttributeSettable` 校验并在不可写时无副作用忽略。

---

## 2. 领域模型与状态机（`principle-model-the-domain`）

所有核心状态收敛为显式值类型与单一状态机，避免散落的布尔标志：

```swift
public struct WindowResizePreferences: Codable, Equatable, Sendable {
    public var isEnabled: Bool = false
    public var nearestCornerResize: Bool = false
    public var excludedBundleIds: [String] = []
}

public enum ResizeAnchorCorner: Equatable, Sendable {
    case topLeft, topRight, bottomLeft, bottomRight
}

public enum WindowPlacementAction: String, CaseIterable, Sendable {
    case tileLeft, tileRight, tileTop, tileBottom
    case fill, center, restorePrevious
}

public enum SplitScreenGestureAction: Equatable, Sendable {
    case splitLeft
    case splitRight
    case enterFullScreen
    case exitFullScreenOrSplit
}

public enum WindowResizeSessionState: Equatable, Sendable {
    case idle
    case moving(target: WindowTargetSnapshot, startPointer: CGPoint, initialOrigin: CGPoint)
    case resizing(target: WindowTargetSnapshot, corner: ResizeAnchorCorner, startPointer: CGPoint, initialFrame: CGRect)
    case splitGesture(target: WindowTargetSnapshot, startPointer: CGPoint, pendingAction: SplitScreenGestureAction?)
}
```

### 状态转移不变量

- `idle` 仅在以下条件全部满足时转移：`isEnabled == true`、无快捷键录制中、目标窗口所属 `bundleIdentifier` 不在 `excludedBundleIds`、目标窗口支持对应属性写权限（移动/缩放）。
- 修饰键实时升降级：处于 `moving` 时按下 `option` 平滑切换到 `resizing`（以当前指针和当前窗口 frame 重新立锚，避免跳变）；松开 `option` 切回 `moving`；松开 `fn` 或 `ctrl` 立即回到 `idle` 并恢复 `AXEnhancedUserInterface`。
- `splitGesture` 期间按下 `Esc` 立即取消并回到 `idle`，吞掉该 `Esc`；松开修饰键或结束拖动时，若 `pendingAction != nil` 则在脱离 tap 回调的异步主线程任务中执行原生动作，绝不在 `CGEventTap` 回调内阻塞执行耗时 AX 调用（避免触发系统 `.tapDisabledByTimeout`）。
- 收到 `onTapReset()`（系统超时或权限被撤）时，无论处于何种状态，立刻恢复 `AXEnhancedUserInterface`、隐藏手势 HUD 并回归 `idle`。

---

## 3. 与 `INTEGRATION.md` 的真实代码接缝

| 现有文件 / 符号 | 集成方式 |
| --- | --- |
| `macos/Sources/CodexAgySyncCore/PreferencesStore.swift`（`AppPreferences`、`PreferencesStore`） | 向 `AppPreferences` 增加向后兼容字段 `windowResize: WindowResizePreferences`（默认关闭），在 `PreferencesStore` 暴露 `setWindowResizePreferences` / `setWindowResizeEnabled` / `setWindowResizeNearestCorner` / `setWindowResizeExcludedBundleIds`。 |
| `macos/Sources/CodexAgySync/GlobalHotKey.swift` 与 `StatusBarController.swift`（`start()`、`applyHotKeyPreferences()`、`stop()`、`onPrepareForRelaunch`） | 将单槽 `GlobalHotKey` 升格为 `DesktopInputHub` 内的 `CarbonHotKeyRegistry`（保持 `id = 1` 映射 `⌃⌥H` 不变）；`StatusBarController` 持有单个 `DesktopInputHub` 实例并在偏好变更、快捷键录制、应用重拉起与退出时统一协调。 |
| `macos/Sources/CodexAgySyncCore/SettingsView.swift`（`SettingsTab`、`SettingsView`） | 新增 `.desktopTools` 标签页，承载权限状态卡片与 `WindowResizeSettingsCard`。 |
| `macos/Sources/CodexAgySyncCore/Localization.swift`（`AppCopy`、`AppCopyKey`） | 新增窗口调整、权限状态、排除应用与冲突提示的中英文文案键，零禁用词。 |
| `macos/Package.swift` | 如有必要显式链接 `ApplicationServices`（`CoreGraphics` 与 `AppKit` 已随 macOS SDK 提供），保持 Swift 6 严格并发编译通过。 |

### 新增文件清单（全部在 `macos/` 下）

- `macos/Sources/CodexAgySyncCore/DesktopInput/`：
  - `DesktopInputTypes.swift`（事件类型、路由决策、冲突诊断模型）
  - `DesktopPermissionService.swift`（AX + Input Monitoring 检查、提示与深链）
  - `ExcludedAppMatcher.swift`（按 bundle ID 匹配与规范化）
  - `ShortcutConflictDetector.swift`（内部绑定 + Carbon 返回码 + `CopySymbolicHotKeys` 检测）
  - `CarbonHotKeyRegistry.swift`（多槽 Carbon 热键管理器）
  - `SharedEventTapRouter.swift`（单实例按需 `CGEventTap` 路由器）
  - `DesktopInputHub.swift`（统一注册、启停、录制暂停与状态协调）
- `macos/Sources/CodexAgySyncCore/WindowResize/`：
  - `WindowResizeModels.swift`（`WindowResizePreferences`、`WindowPlacementAction`、`SplitScreenGestureAction`、`WindowResizeResult`）
  - `WindowGeometry.swift`（纯 Quartz 坐标移动、最近角/右下角缩放与死区方向判定）
  - `WindowResizeStateMachine.swift`（纯状态机，零 AppKit/AX 依赖，全量单测）
  - `AXWindowDriver.swift`（指针下窗口解析、`AXEnhancedUserInterface` 守卫、几何读写、`AXFullScreen` 与双侧退出）
  - `NativeTilingBridge.swift`（`Window -> Move & Resize` 与 `AXFullScreenButton` 原生菜单动作执行与差异回报）
  - `WindowResizeConsumer.swift`（对接 `DesktopInputHub` 的事件消费者）
  - `DesktopToolsSettingsPane.swift`（桌面工具标签页、权限卡、窗口调整设置卡与共享排除应用编辑器）
- `macos/Sources/CodexAgySync/GestureHUDController.swift`（非激活悬浮方向提示层）
- `macos/Tests/CodexAgySyncTests/DesktopInputHubTests.swift` 与 `WindowResizeTests.swift`（使用 `makeIsolatedTestDefaultsSuite`）

---

## 4. 权限、排除名单、冲突与失败模式

### 4.1 权限流程
- 开启窗口调整开关时检测 `DesktopPermissionService`（辅助功能 + 输入监控）。
- 权限缺失时：允许保存开关偏好但不激活 `CGEventTap`，状态显示“需要辅助功能与输入监控权限”，提供“请求权限”与“打开系统设置”按钮以及“重新检测”按钮。
- 运行中权限被撤销时：`SharedEventTapRouter` 停止监听、透传所有输入、不弹窗阻断现有同步功能。

### 4.2 排除名单透传规则
- **移动、缩放、分屏手势**：在手势起始瞬间解析指针下窗口所属应用的 `bundleIdentifier`（无法解析时回退前台应用 `bundleIdentifier`）；若命中 `excludedBundleIds`，立即返回 `.passThrough`，绝不吞键或吞鼠标事件。
- **键盘摆放（`fn + ctrl + 方向键/F/C/R`）**：检查当前前台活跃应用（`NSWorkspace.shared.frontmostApplication?.bundleIdentifier`）；若命中 `excludedBundleIds`，立即返回 `.passThrough`。

### 4.3 失败模式矩阵

| 失败场景 | 预期行为 |
| --- | --- |
| 辅助功能或输入监控权限关闭/被撤销 | 窗口调整安全暂停，事件全部透传，设置页显示权限提示；同步功能与 `⌃⌥H` 零影响 |
| 指针下为桌面、菜单栏、不可移动或不可调整尺寸的窗口 | `AXUIElementIsAttributeSettable` 返回 `false` 时不进入拖动/缩放状态，透传事件或无副作用忽略 |
| 目标应用不支持全屏或原生 Split View（如设置弹窗、自定义无标准窗口菜单的 Electron 窗口） | 原生调用返回 `.unsupportedByTargetApp`，不伪造近似 resize，静默复位并记录诊断状态 |
| 多显示器 / 多 Space | 统一采用 Quartz 全局坐标系计算位移与尺寸；双侧退出分屏按同显示器在屏全屏窗口匹配 |
| Stage Manager 开启 | 原生 Split View 菜单项可能被系统禁用或隐藏，返回 `.unsupportedBySystemState`，不伪造缩放 |
| `CGEventTap` 超时禁用（`.tapDisabledByTimeout`） | 路由器重启用 tap，重置拖动/手势状态机并恢复 `AXEnhancedUserInterface` |
| 第三方外接键盘 `Fn` 不上报 `maskSecondaryFn` | 无法由软件感知；在设置页帮助文案中明确提示，不影响其他快捷键与同步功能 |

---

## 5. 公开 API 与 macOS 系统原生的诚实差异清单

本规格严格禁止用自定义 `AXPosition`/`AXSize` 冒充系统原生平铺或 Split View。以下为公开 API 下的客观边界：

1. **系统原生平铺（`fn + ctrl + 方向键 / F / C / R`）**：
   - 在 **macOS 15+ 且具备标准 AppKit `Window -> Move & Resize` 菜单项的应用**中，通过 `AXMenuItem` `kAXPressAction` 触发的是**100% 真正的系统原生平铺**（原生间距、原生还原记忆）。
   - **差异 1（macOS 14）**：`macos/Package.swift` 声明支持 macOS 14+，但 macOS 14 无系统原生 `Move & Resize` 平铺菜单。在 macOS 14 上无法调用系统原生平铺，将诚实回报不支持，不冒充原生平铺。
   - **差异 2（非标准菜单栏应用）**：部分跨平台/游戏/自定义标题栏应用若未接入系统 `Window -> Move & Resize` 菜单项，公开 AX API 无法调用系统原生平铺器，将诚实回报该应用不支持原生平铺。
2. **系统原生 Split View（`fn + ctrl + option + cmd + 左/右拖动`）**：
   - 在支持全屏且暴露 `AXFullScreenButton` 菜单或 `Window -> Full Screen Tile / Tile Window to Left/Right of Screen` 的应用中，经 AX 触发的是**真正的系统原生 Split View**，由系统接管另一侧窗口选择与 `Esc` 取消。
   - **差异 3（后台非活跃窗口）**：公开 AX API 触发原生 Split View 菜单可能要求目标应用处于活跃态或菜单栏可访问；若无法在不激活状态下触发，需在触发瞬间激活该窗口（实机验证后记录于验收报告）。
   - **差异 4（退出分屏双侧）**：公开 API 无法直接读取私有 SkyLight/CGS Space 分屏配对 ID。本实现通过扫描同一显示器上处于 `AXFullScreen == true` 的在屏窗口一并退出。若另一侧应用拒绝 AX 访问或采用非标准全屏，可能仅退出当前侧窗口，届时在诊断状态中诚实记录。

---

## 6. 在线检索来源及采用/不采用理由

| 来源 | 检索内容 | 采用 / 不采用及理由 |
| --- | --- | --- |
| Apple Developer Documentation: Application Services Accessibility (`AXIsProcessTrustedWithOptions`, `AXUIElementCopyElementAtPosition`, `kAXPositionAttribute`, `kAXSizeAttribute`, `kAXFullScreenAttribute`, `AXZoomButton`/`AXFullScreenButton`) | 权限检查、指针下元素定位、几何属性读写、全屏与绿钮菜单操作 | **采用**：作为全部窗口操作与原生平铺/分屏的唯一公开 API 基础，零私有 SkyLight/CGS 符号。 |
| Apple Developer Documentation: CoreGraphics & Carbon (`CGEvent.tapCreate`, `CGEventFlags.maskSecondaryFn`, `CGPreflightListenEventAccess`, `CopySymbolicHotKeys`, `RegisterEventHotKey`) | 全局事件拦截、`fn` 修饰键位、输入监控权限预检、系统快捷键查询与 Carbon 修饰键限制 | **采用**：证实 Carbon `RegisterEventHotKey` 不支持 `fn`，因此决定 `DesktopInputHub` 采用 `CarbonHotKeyRegistry` + `SharedEventTapRouter` 双底层共存。 |
| Apple macOS Sequoia Window Tiling 官方说明（`Window -> Move & Resize` 与 `Globe + Ctrl + 方向键/F/C/R`） | 系统原生平铺快捷键与菜单层级、Tiling 与 Split View 差异 | **采用**：证实系统原生平铺即 `Window -> Move & Resize` 菜单项，优先走 AX 菜单触发以保证真正的系统原生平铺而非近似缩放。 |
| `pablopunk/SwiftShift`（开源 Swift） | `AXUIElementCopyElementAtPosition` + 无点击指针下窗口移动 + 四象限最近角缩放 | **参考采用算法与 AX 定位模式**：其象限锚点模型简洁且契合 `nearest corner` 需求；不复用其代码，且在 `DesktopInputHub` 下统一管理事件 tap。 |
| `TypoStudio/snatch` 与 `rxhanson/Rectangle`（开源 Swift） | Electron/Chromium 应用拖动卡顿的 `AXEnhancedUserInterface` 临时关闭/恢复方案 | **采用该公开技巧**：可显著改善 VS Code、Chrome、Codex Desktop、Antigravity 等 Electron 窗口的拖动跟手性，并在结束/取消时成对恢复。 |
| 私有 SkyLight / `CGS*` 窗口管理 API（常见于 yabai 等工具） | 直接操纵 Mission Control Space 与 Split View 配对 | **不采用**：依赖非公开符号且部分功能要求关闭 SIP，违反公开 API 干净室与安全公证发布要求。 |

---

## 7. 验收计划（实现完成后逐项执行并留存证据）

1. **自动单元测试**：
   - `DesktopInputHubTests`：验证默关零 tap、录制暂停、排除名单透传、单次消费不重复处理、内部与符号热键冲突检测、现有 `⌃⌥H` 槽位兼容。
   - `WindowResizeTests`：验证 `WindowResizeStateMachine` 全状态转换（移动、切缩放、切回移动、最近角四象限、固定右下角、最小尺寸钳制、分屏手势死区、`Esc` 取消、tap 超时复位）。
2. **全仓门禁**：运行 `./scripts/check` 全量 6 步（含 `cfprefsd` 7.5 秒零泄漏、零普通注释、`lsregister` 零残留）。
3. **实机手工逐项核对（6 条截图事实 + 回归）**：
   - 未启用模块时确认零弹窗、零事件拦截、同步面板 `⌃⌥H` 正常。
   - 启用后依次实测：指针下非活跃窗口移动、加 `option` 缩放、`nearest corner` 开/关对比、活跃窗口 `fn+ctrl+方向键/F/C/R` 原生平铺/填充/居中/还原、`fn+ctrl+option+cmd` 四方向手势（松手应用与 `Esc` 取消、双侧退出分屏）、排除 App 透传、撤销权限安全退化不吞键。
   - 实测记录与差异表写入 `walkthrough.md` 及验收报告，绝不把个别成功案例宣称为完全等价。
