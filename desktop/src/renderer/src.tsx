import React from 'react'
import { createRoot } from 'react-dom/client'
import '@fontsource/inter/400.css'
import '@fontsource/inter/500.css'
import '@fontsource/inter/600.css'
import '@fontsource/inter/700.css'
import './globals.css'
import './app.css'
import './workspace.css'
import App from './App'
import { syncWindowBackdrop } from './lib/window-backdrop'

document.documentElement.dataset.platform = window.palette.platform
try {
  const theme = localStorage.getItem('palette.theme.v1')
  document.documentElement.classList.toggle('dark', theme === 'dark' || theme !== 'light' && matchMedia('(prefers-color-scheme: dark)').matches)
} catch { document.documentElement.classList.toggle('dark', matchMedia('(prefers-color-scheme: dark)').matches) }
syncWindowBackdrop()
createRoot(document.getElementById('root')!).render(<React.StrictMode><App /></React.StrictMode>)
