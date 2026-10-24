/* Inline icon set — geometric, 1.8px stroke, drawn for this console.
   No icon dependency; every glyph shares the same optical grid. */

type IconProps = { size?: number }

const base = (size: number) => ({
  width: size,
  height: size,
  viewBox: '0 0 24 24',
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 1.8,
  strokeLinecap: 'round' as const,
  strokeLinejoin: 'round' as const,
  'aria-hidden': true,
})

export const PlusIcon = ({ size = 16 }: IconProps) => (
  <svg {...base(size)}>
    <path d="M12 4v16M4 12h16" />
  </svg>
)

export const GearIcon = ({ size = 16 }: IconProps) => (
  <svg {...base(size)}>
    <circle cx="12" cy="12" r="3.2" />
    <path d="M12 2.8v2.4M12 18.8v2.4M4.6 7.2l2 1.2M17.4 15.6l2 1.2M4.6 16.8l2-1.2M17.4 8.4l2-1.2" />
  </svg>
)

export const SunIcon = ({ size = 16 }: IconProps) => (
  <svg {...base(size)}>
    <circle cx="12" cy="12" r="4" />
    <path d="M12 2.5v2M12 19.5v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2.5 12h2M19.5 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
  </svg>
)

export const MoonIcon = ({ size = 16 }: IconProps) => (
  <svg {...base(size)}>
    <path d="M20.6 13.2A8.8 8.8 0 1 1 10.8 3.4a6.8 6.8 0 0 0 9.8 9.8z" />
  </svg>
)

export const MenuIcon = ({ size = 18 }: IconProps) => (
  <svg {...base(size)}>
    <path d="M3.5 6.5h17M3.5 12h17M3.5 17.5h17" />
  </svg>
)

export const SearchIcon = ({ size = 14 }: IconProps) => (
  <svg {...base(size)}>
    <circle cx="10.5" cy="10.5" r="6.5" />
    <path d="M15.5 15.5L21 21" />
  </svg>
)

export const CloseIcon = ({ size = 14 }: IconProps) => (
  <svg {...base(size)}>
    <path d="M5 5l14 14M19 5L5 19" />
  </svg>
)

export const PlayIcon = ({ size = 15 }: IconProps) => (
  <svg {...base(size)}>
    <path d="M7 4.5l12 7.5-12 7.5v-15z" />
  </svg>
)

export const StopIcon = ({ size = 14 }: IconProps) => (
  <svg {...base(size)}>
    <rect x="6" y="6" width="12" height="12" rx="1.5" />
  </svg>
)

export const ExportIcon = ({ size = 15 }: IconProps) => (
  <svg {...base(size)}>
    <path d="M12 3.5v11M7.5 9.5L12 14l4.5-4.5" />
    <path d="M4.5 17.5v1.5a1.5 1.5 0 0 0 1.5 1.5h12a1.5 1.5 0 0 0 1.5-1.5v-1.5" />
  </svg>
)

export const ExternalIcon = ({ size = 12 }: IconProps) => (
  <svg {...base(size)}>
    <path d="M9.5 5.5H5.5v13h13v-4" />
    <path d="M14 4.5h5.5V10M19.5 4.5L10.5 13.5" />
  </svg>
)

export const FileIcon = ({ size = 15 }: IconProps) => (
  <svg {...base(size)}>
    <path d="M6.5 3.5h7l4 4v13h-11v-17z" />
    <path d="M13.5 3.5v4h4" />
  </svg>
)

export const ArrowLeftIcon = ({ size = 15 }: IconProps) => (
  <svg {...base(size)}>
    <path d="M4.5 12h15M10 5.5L4.5 12 10 18.5" />
  </svg>
)

/* Brand insignia — orbit ring + crosshair, the console's signature mark. */
export const Insignia = ({ size = 30 }: IconProps) => (
  <svg
    width={size}
    height={size}
    viewBox="0 0 32 32"
    fill="none"
    aria-hidden
    style={{ flexShrink: 0 }}
  >
    <circle cx="16" cy="16" r="12.5" stroke="currentColor" strokeWidth="1.4" opacity="0.45" />
    <circle cx="16" cy="16" r="7.5" stroke="currentColor" strokeWidth="1.8" />
    <circle cx="16" cy="16" r="2.6" fill="currentColor" />
    <path
      d="M16 1.5v5M16 25.5v5M1.5 16h5M25.5 16h5"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
    />
    <circle cx="27.2" cy="9.8" r="1.7" fill="currentColor" />
  </svg>
)
