export const isTelegramMiniApp = () =>
  typeof window !== 'undefined' && !!window.Telegram?.WebApp?.initData

export const getTelegramInitData = () =>
  window.Telegram?.WebApp?.initData || ''

export const getTelegramTheme = () =>
  isTelegramMiniApp() ? window.Telegram.WebApp.themeParams : null

export function initTelegramApp() {
  if (!isTelegramMiniApp()) return
  const tg = window.Telegram.WebApp
  tg.ready()
  tg.expand()

  // Mark the root so CSS knows we're inside Telegram (disables prefers-color-scheme override)
  document.documentElement.setAttribute('data-tg-theme', tg.colorScheme || 'light')

  // Sync theme if it changes (user flips Telegram dark/light while app is open)
  tg.onEvent('themeChanged', () => {
    document.documentElement.setAttribute('data-tg-theme', tg.colorScheme || 'light')
  })

  // Handle viewport resizes (keyboard appearing, etc.)
  tg.onEvent('viewportChanged', () => {
    document.documentElement.style.setProperty(
      '--tg-viewport-height',
      tg.viewportStableHeight ? tg.viewportStableHeight + 'px' : '100dvh'
    )
  })
}

/**
 * Trigger haptic feedback. Silently ignored when not in Telegram.
 * @param {'light'|'medium'|'heavy'|'rigid'|'soft'} style
 */
export function haptic(style = 'light') {
  try {
    window.Telegram?.WebApp?.HapticFeedback?.impactOccurred(style)
  } catch (_) {}
}

/**
 * Trigger a notification haptic.
 * @param {'error'|'success'|'warning'} type
 */
export function hapticNotify(type = 'success') {
  try {
    window.Telegram?.WebApp?.HapticFeedback?.notificationOccurred(type)
  } catch (_) {}
}
