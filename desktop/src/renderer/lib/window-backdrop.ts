/** Native Windows controls are painted above the DOM backdrop. */
export function syncWindowBackdrop(): void {
  if (window.palette.platform !== 'win32') return
  let frame = 0
  let previousShade = ''

  function schedule(): void {
    if (!frame) frame = requestAnimationFrame(update)
  }

  function update(): void {
    frame = 0
    let transmission = 1
    let animating = false
    for (const backdrop of document.querySelectorAll<HTMLElement>('[data-window-backdrop]')) {
      const style = getComputedStyle(backdrop)
      if (style.display === 'none' || style.visibility === 'hidden') continue
      const opacity = Number(style.opacity) * Number(style.getPropertyValue('--dialog-backdrop-opacity'))
      transmission *= 1 - opacity
      animating ||= backdrop.getAnimations().some(animation => animation.playState === 'running')
    }
    const background = getComputedStyle(document.documentElement).getPropertyValue('--background').trim()
    const base = /^#[a-f0-9]{6}$/i.test(background) ? background : '#f7f6f5'
    const shade = `${base}:${Math.round(transmission * 1000)}`
    if (shade !== previousShade) {
      previousShade = shade
      void window.palette.setWindowBackdrop(1 - transmission, base).catch(error => console.error('Window backdrop update failed', error))
    }
    if (animating) schedule()
  }

  const observer = new MutationObserver(schedule)
  observer.observe(document.documentElement, { childList: true, subtree: true, attributes: true, attributeFilter: ['style', 'class', 'data-theme', 'hidden', 'data-starting-style', 'data-ending-style'] })
  window.addEventListener('pagehide', () => { observer.disconnect(); cancelAnimationFrame(frame) }, { once: true })
  schedule()
}
