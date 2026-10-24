import { useEffect, useState } from 'react'
import { Insignia, GearIcon, MoonIcon, PlusIcon, SunIcon } from './Icons'
import { utcClockText } from '../lib/format'
import type { Theme } from '../lib/types'

type TopBarProps = {
  theme: Theme
  onToggleTheme: () => void
  onOpenSettings: () => void
  onNewReview: () => void
  onToggleRail: () => void
  railOpen: boolean
  demoMode: boolean
  menuIcon: React.ReactNode
}

/* Command bar — brand, live UTC clock, theme / settings / new review.
   The UTC clock is the console's ambient "carrier signal": it proves the
   session is live without the user having to look at any data. */
export function TopBar({
  theme,
  onToggleTheme,
  onOpenSettings,
  onNewReview,
  onToggleRail,
  railOpen,
  demoMode,
  menuIcon,
}: TopBarProps) {
  const [clock, setClock] = useState(() => utcClockText(new Date()))

  useEffect(() => {
    const timer = window.setInterval(() => setClock(utcClockText(new Date())), 1000)
    return () => window.clearInterval(timer)
  }, [])

  return (
    <header className="cmdbar">
      <button
        className="icon-btn rail-toggle"
        onClick={onToggleRail}
        aria-label="切换任务队列"
        aria-expanded={railOpen}
      >
        {menuIcon}
      </button>

      <div className="brand">
        <Insignia />
        <span className="brand-text">
          <span className="brand-name">MISSION CONTROL</span>
          <span className="brand-sub">AI 代码审查控制台</span>
        </span>
      </div>

      <div className="cmdbar-right">
        {demoMode && <span className="demo-chip">演示数据 demo</span>}
        <div className="clock" title="协调世界时 UTC">
          <span className="clock-label">UTC</span>
          <span className="clock-time">{clock}</span>
        </div>
        <span className="v-divider" aria-hidden="true" />
        <button
          className="icon-btn"
          onClick={onToggleTheme}
          aria-label={theme === 'dark' ? '切换到浅色主题' : '切换到深色主题'}
          title={theme === 'dark' ? '浅色主题' : '深色主题'}
        >
          {theme === 'dark' ? <SunIcon /> : <MoonIcon />}
        </button>
        <button className="btn btn-ghost" onClick={onOpenSettings}>
          <GearIcon />
          设置
        </button>
        <button className="btn btn-primary" onClick={onNewReview}>
          <PlusIcon />
          新建审查
        </button>
      </div>
    </header>
  )
}
