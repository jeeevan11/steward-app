import SwiftUI
import AppKit

/// The Steward mark — a ring that fills with significance. The ONLY status in the product.
///   calm ○  ·  one ◔  ·  several ◑  ·  important ● (warm)
/// Rendered as a template NSImage for the menu bar (so macOS tints it) and as a SwiftUI
/// view for in-app use.
enum Glyph {
    /// Menu-bar icon: a template image. Important state is non-template + warm.
    static func barImage(_ s: Significance) -> NSImage {
        let size = NSSize(width: 18, height: 18)
        let img = NSImage(size: size)
        img.lockFocus()
        let inset: CGFloat = 3
        let rect = NSRect(x: inset, y: inset, width: size.width - inset * 2, height: size.height - inset * 2)
        let ring = NSBezierPath(ovalIn: rect)
        ring.lineWidth = 1.5
        let color: NSColor = (s == .important) ? NSColor(hex: 0xD4AD77) : .labelColor
        color.setStroke()
        ring.stroke()

        let fill = NSBezierPath()
        let c = NSPoint(x: rect.midX, y: rect.midY)
        let r = rect.width / 2
        switch s {
        case .calm:
            break
        case .one:   pieFill(fill, center: c, radius: r, startDeg: 90, endDeg: 0)     // a quarter
        case .several: pieFill(fill, center: c, radius: r, startDeg: 90, endDeg: -90)  // a half
        case .important:
            NSBezierPath(ovalIn: rect).fill()                                         // whole
        }
        if s == .one || s == .several {
            color.setFill()
            fill.fill()
        }
        img.unlockFocus()
        img.isTemplate = (s != .important)   // important is warm, others tint with the bar
        return img
    }

    private static func pieFill(_ p: NSBezierPath, center: NSPoint, radius: CGFloat,
                                startDeg: CGFloat, endDeg: CGFloat) {
        p.move(to: center)
        p.appendArc(withCenter: center, radius: radius, startAngle: startDeg, endAngle: endDeg, clockwise: true)
        p.close()
    }
}

/// In-app SwiftUI glyph.
struct GlyphView: View {
    let significance: Significance
    var size: CGFloat = 16

    var body: some View {
        ZStack {
            Circle().stroke(ringColor, lineWidth: 1.4)
            switch significance {
            case .calm: EmptyView()
            case .one: Wedge(fraction: 0.25).fill(ringColor)
            case .several: Wedge(fraction: 0.5).fill(ringColor)
            case .important: Circle().fill(Steward.C.amber)
            }
        }
        .frame(width: size, height: size)
    }

    private var ringColor: Color {
        switch significance {
        case .calm: return Steward.C.t3
        case .one: return Steward.C.t2
        case .several: return Color(hex: 0xCFCABF)
        case .important: return Steward.C.amber
        }
    }
}

private struct Wedge: Shape {
    let fraction: Double
    func path(in rect: CGRect) -> Path {
        var p = Path()
        let c = CGPoint(x: rect.midX, y: rect.midY)
        p.move(to: c)
        p.addArc(center: c, radius: rect.width / 2,
                 startAngle: .degrees(-90),
                 endAngle: .degrees(-90 + 360 * fraction), clockwise: false)
        p.closeSubpath()
        return p
    }
}
