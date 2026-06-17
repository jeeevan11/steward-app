import SwiftUI
import WidgetKit

/// Reusable widget content — the four significance states. Importance, never activity.
/// These views are ready to drop into a WidgetKit extension target (a TimelineProvider
/// emitting `Significance` + an optional headline decision). See STEWARD_DESIGN.md §15:
/// the extension itself needs an Xcode app-extension target (not buildable from SPM),
/// so the views live here and are previewed in-app via `StewardWidgetPreview`.
struct StewardWidgetView: View {
    let significance: Significance
    let primary: String?      // headline decision title, if any
    let line: String?         // one supporting sentence
    let urgent: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 14) {
                GlyphView(significance: significance, size: 20)
                Spacer()
                if significance != .calm { widgetClear }
                widgetRefresh
                widgetOpen
            }
            Spacer(minLength: 10)
            Text(title).font(.system(size: 19, weight: .light)).foregroundColor(Steward.C.tx)
                .fixedSize(horizontal: false, vertical: true)
            if let line {
                HStack(spacing: 8) {
                    if significance != .calm {
                        Circle().fill(urgent ? Steward.C.amber : Steward.C.blue).frame(width: 7, height: 7)
                    }
                    Text(line).font(Steward.F.support).foregroundColor(urgent ? Steward.C.amber : Steward.C.t2)
                        .lineLimit(2)
                }.padding(.top, 8)
            }
        }
        .padding(22)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(Steward.C.surface)
        .clipShape(RoundedRectangle(cornerRadius: 18))
        .environment(\.colorScheme, .dark)
        .widgetURL(URL(string: "steward://open"))
    }

    /// Refresh affordance. In a WidgetKit extension (macOS 14+) this is a real
    /// `Button(intent:)` that reloads the timeline; in-app it refreshes the store.
    @ViewBuilder private var widgetRefresh: some View {
        if #available(macOS 14.0, *) {
            Button(intent: RefreshSteward()) {
                Image(systemName: "arrow.clockwise").font(.system(size: 12)).foregroundColor(Steward.C.t3)
            }.buttonStyle(.plain)
        } else {
            Button { StewardStore.shared.refresh(manual: true) } label: {
                Image(systemName: "arrow.clockwise").font(.system(size: 12)).foregroundColor(Steward.C.t3)
            }.buttonStyle(.plain)
        }
    }

    /// Open the Steward dashboard. In a real widget the whole tile also deep-links via
    /// `.widgetURL(steward://open)`; this icon is the explicit affordance.
    @ViewBuilder private var widgetOpen: some View {
        Button { StewardWindow.shared.show() } label: {
            Image(systemName: "arrow.up.forward.app").font(.system(size: 12)).foregroundColor(Steward.C.t3)
        }.buttonStyle(.plain)
    }

    @ViewBuilder private var widgetClear: some View {
        if #available(macOS 14.0, *) {
            Button(intent: ClearAllSteward()) {
                Image(systemName: "checklist").font(.system(size: 12)).foregroundColor(Steward.C.t3)
            }.buttonStyle(.plain)
        } else {
            Button { StewardStore.shared.clearAll() } label: {
                Image(systemName: "checklist").font(.system(size: 12)).foregroundColor(Steward.C.t3)
            }.buttonStyle(.plain)
        }
    }

    private var title: String {
        switch significance {
        case .calm: return "Everything handled."
        case .one: return "One thing needs you."
        case .several: return "Two decisions await."
        case .important: return urgent ? "A decision is due." : "Needs your attention."
        }
    }
}

/// An in-app preview board of all four states (so the widget can be reviewed without
/// installing the extension).
struct StewardWidgetPreview: View {
    var body: some View {
        let tiles: [(Significance, String?, String?, Bool)] = [
            (.calm, nil, "Nothing needs your attention.", false),
            (.one, nil, "Investor follow-up", false),
            (.several, nil, "University application", false),
            (.important, nil, "Application deadline tomorrow.", true),
        ]
        LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible())], spacing: 14) {
            ForEach(0..<tiles.count, id: \.self) { i in
                StewardWidgetView(significance: tiles[i].0, primary: tiles[i].1,
                                  line: tiles[i].2, urgent: tiles[i].3)
                    .frame(height: 150)
            }
        }
        .padding(16)
        .background(Steward.C.canvas)
    }
}
