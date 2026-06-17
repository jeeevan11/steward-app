import SwiftUI
import WidgetKit
import AppIntents

/// A quiet refresh control: a circular-arrow that rotates continuously while loading.
struct RefreshButton: View {
    @ObservedObject var store = StewardStore.shared
    @State private var spin = false

    var body: some View {
        Button {
            store.refresh(manual: true)
        } label: {
            Image(systemName: "arrow.clockwise")
                .font(.system(size: 13, weight: .regular))
                .foregroundColor(store.loading ? Steward.C.t2 : Steward.C.t3)
                .rotationEffect(.degrees(spin ? 360 : 0))
        }
        .buttonStyle(.plain)
        .help("Refresh")
        .onChange(of: store.loading) { loading in
            if loading {
                spin = false
                withAnimation(.linear(duration: 0.8).repeatForever(autoreverses: false)) { spin = true }
            } else {
                withAnimation(.easeOut(duration: 0.2)) { spin = false }
            }
        }
    }
}

/// Subtle hover lift for interactive rows/buttons (smoothness without noise).
struct HoverHighlight: ViewModifier {
    @State private var hovering = false
    var radius: CGFloat = 10
    func body(content: Content) -> some View {
        content
            .background(RoundedRectangle(cornerRadius: radius)
                .fill(Color.white.opacity(hovering ? 0.04 : 0)))
            .animation(.easeOut(duration: 0.18), value: hovering)
            .onHover { hovering = $0 }
    }
}

extension View {
    func hoverHighlight(radius: CGFloat = 10) -> some View { modifier(HoverHighlight(radius: radius)) }
}

/// Widget refresh (AppIntent). In a real WidgetKit extension this backs a
/// `Button(intent: RefreshSteward())`; macOS reloads the widget timeline when it runs.
struct RefreshSteward: AppIntent {
    static var title: LocalizedStringResource = "Refresh Steward"
    func perform() async throws -> some IntentResult {
        WidgetCenter.shared.reloadAllTimelines()
        return .result()
    }
}

/// Clear all from the widget — skips every awaiting decision via the engine, then reloads.
struct ClearAllSteward: AppIntent {
    static var title: LocalizedStringResource = "Clear all decisions"
    func perform() async throws -> some IntentResult {
        let base = AppConfig.shared.dashboardURL
        if let url = URL(string: base + "/api/decisions/clear") {
            var req = URLRequest(url: url); req.httpMethod = "POST"
            let token = AppConfig.shared.envValue("CONSOLE_TOKEN") ?? ""
            if !token.isEmpty { req.setValue(token, forHTTPHeaderField: "X-Cos-Token") }
            _ = try? await URLSession.shared.data(for: req)
        }
        WidgetCenter.shared.reloadAllTimelines()
        return .result()
    }
}
