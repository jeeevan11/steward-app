import SwiftUI
import AppKit

/// Steward design tokens — the single source of truth for the redesigned experience.
/// Dark-first, warm, monochrome; accent appears ONLY on a decision's significance.
/// (See STEWARD_DESIGN.md for the full system.)
enum Steward {
    enum C {
        static let canvas  = Color(hex: 0x1B1A18)   // warm near-black
        static let surface = Color(hex: 0x232120)
        static let raised  = Color(hex: 0x26221F)
        static let line    = Color.white.opacity(0.08)
        static let tx      = Color(hex: 0xF2EFE9)    // primary text
        static let t2      = Color(hex: 0xA7A399)    // secondary
        static let t3      = Color(hex: 0x6B675E)    // tertiary
        static let blue    = Color(hex: 0x7FA6CE)    // tier 2 — decide when you can
        static let amber   = Color(hex: 0xD4AD77)    // tier 3 — decide soon / overdue
        static let onLight = Color(hex: 0xEFECE6)    // primary-action fill
    }

    enum F {
        static func hero(_ size: CGFloat = 42) -> Font { .system(size: size, weight: .light) }
        static let titleLg = Font.system(size: 30, weight: .light)
        static let title   = Font.system(size: 22, weight: .regular)
        static let popHero = Font.system(size: 21, weight: .light)
        static let body    = Font.system(size: 17, weight: .light)
        static let support = Font.system(size: 15, weight: .regular)
        static let meta    = Font.system(size: 13, weight: .regular)
        static let label   = Font.system(size: 12, weight: .regular)
    }

    enum S {
        static let xs: CGFloat = 8
        static let sm: CGFloat = 12
        static let md: CGFloat = 16
        static let lg: CGFloat = 24
        static let xl: CGFloat = 32
        static let xxl: CGFloat = 48
        static let hero: CGFloat = 56
    }

    enum M {  // motion
        static let standard = 0.28
        static let screen = 0.34
    }

    /// Accent for a tier (2 = blue, 3 = amber). No accent is the calm default.
    static func accent(forTier tier: Int) -> Color { tier >= 3 ? C.amber : C.blue }
}

extension Color {
    init(hex: UInt) {
        self.init(.sRGB,
                  red: Double((hex >> 16) & 0xFF) / 255.0,
                  green: Double((hex >> 8) & 0xFF) / 255.0,
                  blue: Double(hex & 0xFF) / 255.0,
                  opacity: 1.0)
    }
}

extension NSColor {
    convenience init(hex: UInt) {
        self.init(srgbRed: CGFloat((hex >> 16) & 0xFF) / 255.0,
                  green: CGFloat((hex >> 8) & 0xFF) / 255.0,
                  blue: CGFloat(hex & 0xFF) / 255.0, alpha: 1.0)
    }
}
