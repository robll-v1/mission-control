import { ExternalIcon, ExportIcon, FileIcon, PlayIcon, StopIcon } from './Icons'
import {
  EVENT_LIMIT,
  SEVERITY_ORDER,
  durationText,
  previewText,
  severityEnLabel,
  severityLabel,
  severitySegments,
  shortSha,
  stageLabel,
  statusLabel,
  statusTone,
  stringifyPayload,
  timeText,
  relativeTimeText,
  verdictEnLabel,
  verdictLabel,
  missionId,
  prShortLabel,
} from '../lib/format'
import type { DetailTab, EventRecord, ReviewRound, TaskDetail } from '../lib/types'

type MissionPanelProps = {
  detail: TaskDetail
  tab: DetailTab
  onTabChange: (tab: DetailTab) => void
  showAllEvents: boolean
  onToggleAllEvents: () => void
  onStartReview: () => void
  onExport: () => void
  onAbort: () => void
}

/* ── Mission header ─────────────────────────────────────────── */

function MissionHeader({ detail, onStartReview, onExport, onAbort }: MissionPanelProps) {
  const { task, reviewRounds } = detail
  const tone = statusTone(task.status)
  const isRunning = task.status === 'running'
  const prLabel = prShortLabel(task)
  const latestRound = reviewRounds.length ? reviewRounds[reviewRounds.length - 1] : undefined

  return (
    <header className="mission-head boot-up" style={{ animationDelay: '80ms' }}>
      <span className="mission-id">{missionId(task.id)}</span>
      <h1 className="mission-title">{task.title}</h1>
      <div className="mission-eyebrow">
        <span className="status-text">
          <span className={`lamp lamp-${tone}`} aria-hidden="true" />
          {statusLabel(task.status)}
        </span>
        <span className="sep">/</span>
        <span>{stageLabel(task.current_stage)}</span>
        {prLabel && (
          <>
            <span className="sep">/</span>
            {task.source_url ? (
              <a href={task.source_url} target="_blank" rel="noreferrer">
                {prLabel}
                <ExternalIcon />
              </a>
            ) : (
              <span>{prLabel}</span>
            )}
          </>
        )}
      </div>
      <div className="mission-actions">
        <button className="btn btn-primary" onClick={onStartReview} disabled={isRunning}>
          <PlayIcon />
          {latestRound ? '发起下一轮审查' : '开始首轮审查'}
        </button>
        <button className="btn btn-ghost" onClick={onExport}>
          <ExportIcon />
          导出摘要
        </button>
        <button className="btn btn-danger" onClick={onAbort} disabled={!isRunning}>
          <StopIcon />
          中止
        </button>
      </div>
      <div className="meta-row">
        <span className="meta-item">
          <span className="meta-key">repo</span>
          <span className="meta-val mono" title={task.repo_path}>
            {task.repo_path}
          </span>
        </span>
        <span className="meta-item">
          <span className="meta-key">backend</span>
          <span className="meta-val">{task.backend}</span>
        </span>
        <span className="meta-item">
          <span className="meta-key">rounds</span>
          <span className="meta-val mono">{reviewRounds.length}</span>
        </span>
        <span className="meta-item">
          <span className="meta-key">commit</span>
          <span className="meta-val mono">{shortSha(latestRound?.review_revision ?? task.pr_head_sha)}</span>
        </span>
        <span className="meta-item">
          <span className="meta-key">updated</span>
          <span className="meta-val">{relativeTimeText(task.updated_at)}</span>
        </span>
      </div>
    </header>
  )
}

/* ── Verdict panel: stamp + severity meter + stats ──────────── */

function VerdictPanel({ detail }: { detail: TaskDetail }) {
  const result = detail.latestReviewResult
  const verdict = result?.verdict ?? null
  const segments = severitySegments(result)
  const hasFindings = (result?.finding_count ?? 0) > 0

  return (
    <section
      className={`panel verdict-panel boot-up is-${verdict ?? 'inconclusive'}`}
      style={{ animationDelay: '160ms' }}
      aria-label="最新审查结论"
    >
      <span className="brackets" aria-hidden="true" />
      <div className="verdict-grid">
        <div className="verdict-stamp">
          <span className="stamp-kicker">verdict / 审查结论</span>
          <span className="verdict-stamp-word">{verdictEnLabel(verdict)}</span>
          <span className="verdict-stamp-cn">{verdictLabel(verdict)}</span>
        </div>

        <div className="verdict-body">
          <p className={`verdict-summary ${result ? '' : 'is-empty'}`}>
            {result?.summary || '这个任务还没有产出结构化的审查结论。'}
          </p>
          {hasFindings && (
            <div className="sev-meter">
              <span className="micro">
                严重度分布<i>/</i>severity distribution
              </span>
              <div
                className="sev-bar"
                role="img"
                aria-label={segments
                  .map((s) => `${severityLabel(s.severity)} ${s.count} 条`)
                  .join('，')}
              >
                {segments
                  .filter((s) => s.count > 0)
                  .map((s) => (
                    <span
                      key={s.severity}
                      className={`seg seg-${s.severity}`}
                      style={{ width: `${s.percent}%` }}
                      title={`${severityLabel(s.severity)} ${s.count} 条`}
                    />
                  ))}
              </div>
              <div className="sev-legend">
                {segments.map((s) => (
                  <span key={s.severity} className={`lg lg-${s.severity}`}>
                    <i aria-hidden="true" />
                    {severityLabel(s.severity)}
                    <b>{String(s.count).padStart(2, '0')}</b>
                  </span>
                ))}
              </div>
            </div>
          )}
          {result && !hasFindings && (
            <p className="verdict-summary is-empty">本轮未产出结构化问题。</p>
          )}
        </div>

        <div className="verdict-stats">
          <div className="stat">
            <span className="meta-key">findings</span>
            <span className="stat-v">{String(result?.finding_count ?? 0).padStart(2, '0')}</span>
          </div>
          <div className="stat">
            <span className="meta-key">rounds</span>
            <span className="stat-v">{String(detail.reviewRounds.length).padStart(2, '0')}</span>
          </div>
          <div className="stat">
            <span className="meta-key">updated</span>
            <span className="stat-v" style={{ fontSize: 16 }}>
              {relativeTimeText(detail.task.updated_at)}
            </span>
          </div>
        </div>
      </div>
    </section>
  )
}

/* ── Tabs ──────────────────────────────────────────────────── */

const TABS: DetailTab[] = ['results', 'timeline', 'rounds', 'artifacts']
const TAB_NAMES: Record<DetailTab, { cn: string; en: string }> = {
  results: { cn: '审查结果', en: 'findings' },
  timeline: { cn: '事件流', en: 'timeline' },
  rounds: { cn: '审查轮次', en: 'rounds' },
  artifacts: { cn: '产物', en: 'artifacts' },
}

function TabStrip({
  detail,
  tab,
  onTabChange,
}: {
  detail: TaskDetail
  tab: DetailTab
  onTabChange: (t: DetailTab) => void
}) {
  const counts: Record<DetailTab, number> = {
    results: detail.latestReviewResult?.finding_count ?? 0,
    timeline: detail.eventStats.important_count,
    rounds: detail.reviewRounds.length,
    artifacts: detail.artifacts.length,
  }
  return (
    <div className="tabs boot-up" style={{ animationDelay: '220ms' }} role="tablist" aria-label="任务详情页签">
      {TABS.map((id) => (
        <button
          key={id}
          role="tab"
          aria-selected={tab === id}
          className={`tab ${tab === id ? 'active' : ''}`}
          onClick={() => onTabChange(id)}
        >
          {TAB_NAMES[id].cn}
          <span className="micro" style={{ letterSpacing: '0.06em' }}>
            {TAB_NAMES[id].en}
          </span>
          <span className="tab-count">{counts[id]}</span>
        </button>
      ))}
    </div>
  )
}

/* ── Findings ──────────────────────────────────────────────── */

function FindingsList({ detail }: { detail: TaskDetail }) {
  const result = detail.latestReviewResult
  const findings = result?.findings ?? []

  if (!result) {
    return <div className="empty-note">还没有审查结果，先跑一轮吧。</div>
  }
  if (!findings.length) {
    return <div className="empty-note">这一轮没有产出结构化问题。</div>
  }

  return (
    <>
      <div className="section-label">
        <span className="micro">
          问题列表<i>/</i>findings
        </span>
        <span className="micro count">{findings.length} 条</span>
      </div>
      <div className="findings">
        {findings.map((finding, index) => (
          <article className={`finding sev-${finding.severity}`} key={`${finding.severity}-${index}`}>
            <div className="finding-gutter">
              <span className="finding-index">F-{String(index + 1).padStart(2, '0')}</span>
              <span className="sev-tag">
                {severityEnLabel(finding.severity)} / {severityLabel(finding.severity)}
              </span>
            </div>
            <div className="finding-body">
              <span className="finding-loc">
                {finding.path ? `${finding.path}${finding.line ? `:${finding.line}` : ''}` : '未指明位置'}
              </span>
              <h3 className="finding-title">{finding.summary}</h3>
              {finding.detail && finding.detail !== finding.summary && (
                <p className="finding-detail">{finding.detail}</p>
              )}
            </div>
          </article>
        ))}
      </div>
    </>
  )
}

/* ── Event log ─────────────────────────────────────────────── */

function EventRow({ event }: { event: EventRecord }) {
  const fullText = stringifyPayload(event.payload)
  const shortText = previewText(fullText)
  const truncated = shortText !== fullText

  return (
    <article className={`event lvl-${event.level}`}>
      <span className="event-seq">#{String(event.seq).padStart(3, '0')}</span>
      <span className="event-kind">{event.kind}</span>
      <span className="event-text">{shortText}</span>
      <span className="event-time">{timeText(event.ts)}</span>
      {truncated && (
        <details>
          <summary>查看完整输出</summary>
          <pre>{fullText}</pre>
        </details>
      )}
    </article>
  )
}

function Timeline({
  detail,
  showAllEvents,
  onToggleAllEvents,
}: {
  detail: TaskDetail
  showAllEvents: boolean
  onToggleAllEvents: () => void
}) {
  const events = showAllEvents ? detail.events : detail.importantEvents
  const visible = showAllEvents ? events : events.slice(-EVENT_LIMIT)

  return (
    <>
      <div className="timeline-bar">
        <span className="micro">
          {showAllEvents
            ? `全部 ${detail.eventStats.all_count} 条事件`
            : `${detail.eventStats.important_count} 条重要事件`}
          <i>/</i>
          event log
        </span>
        {detail.eventStats.hidden_count > 0 && (
          <button className="btn btn-ghost" onClick={onToggleAllEvents}>
            {showAllEvents ? '仅看重要事件' : `显示全部 ${detail.eventStats.all_count} 条`}
          </button>
        )}
      </div>
      {visible.length ? (
        <div className="events">
          {visible.map((event) => (
            <EventRow key={event.id} event={event} />
          ))}
        </div>
      ) : (
        <div className="empty-note">暂无事件</div>
      )}
    </>
  )
}

/* ── Rounds ────────────────────────────────────────────────── */

function RoundsList({ detail }: { detail: TaskDetail }) {
  const rounds = [...detail.reviewRounds].sort((a, b) => b.round_index - a.round_index)

  if (!rounds.length) {
    return <div className="empty-note">还没有跑过任何一轮审查。</div>
  }

  return (
    <div className="rounds">
      {rounds.map((round) => (
        <RoundCard key={round.id} round={round} />
      ))}
    </div>
  )
}

function RoundCard({ round }: { round: ReviewRound }) {
  const result = round.review_result
  return (
    <article className="round">
      <div className="round-head">
        <span className="round-id">R{round.round_index}</span>
        <span className={`chip chip-${result?.verdict ?? 'idle'}`}>
          {verdictEnLabel(result?.verdict)}
        </span>
        <span className="micro" style={{ letterSpacing: '0.04em' }}>
          {verdictLabel(result?.verdict)}
        </span>
        {(result?.finding_count ?? 0) > 0 && (
          <span className="micro" style={{ marginLeft: 'auto' }}>
            {result?.finding_count} 条问题
          </span>
        )}
      </div>
      {round.review_note && <div className="round-note">{round.review_note}</div>}
      {result?.summary && <p className="round-summary">{result.summary}</p>}
      <div className="kv-grid">
        <div className="kv">
          <span className="meta-key">backend</span>
          <span className="kv-v">{round.backend}</span>
        </div>
        <div className="kv">
          <span className="meta-key">status</span>
          <span className="kv-v">{round.status}</span>
        </div>
        <div className="kv">
          <span className="meta-key">duration</span>
          <span className="kv-v mono">{durationText(round.started_at, round.ended_at)}</span>
        </div>
        <div className="kv">
          <span className="meta-key">exit</span>
          <span className="kv-v mono">{String(round.exit_code ?? '—')}</span>
        </div>
        <div className="kv">
          <span className="meta-key">commit</span>
          <span className="kv-v mono">{shortSha(round.review_revision)}</span>
        </div>
        <div className="kv">
          <span className="meta-key">started</span>
          <span className="kv-v">{timeText(round.started_at)}</span>
        </div>
      </div>
    </article>
  )
}

/* ── Artifacts ─────────────────────────────────────────────── */

function ArtifactsList({ detail }: { detail: TaskDetail }) {
  if (!detail.artifacts.length) {
    return <div className="empty-note">暂无产物文件</div>
  }
  return (
    <div className="artifacts">
      {detail.artifacts.map((artifact) => (
        <div className="artifact" key={artifact.path}>
          <span className="artifact-icon">
            <FileIcon />
          </span>
          <span className="artifact-name">{artifact.relative_path}</span>
          <span className="artifact-path" title={artifact.path}>
            {artifact.path}
          </span>
        </div>
      ))}
    </div>
  )
}

/* ── Panel shell ───────────────────────────────────────────── */

export function MissionPanel(props: MissionPanelProps) {
  const { detail, tab, onTabChange, showAllEvents, onToggleAllEvents } = props

  return (
    <div className="mission">
      <MissionHeader {...props} />
      <VerdictPanel detail={detail} />

      {detail.task.description && (
        <details className="description boot-up" style={{ animationDelay: '240ms' }}>
          <summary>任务背景 / mission brief</summary>
          <pre>{detail.task.description}</pre>
        </details>
      )}

      <TabStrip detail={detail} tab={tab} onTabChange={onTabChange} />

      <div className="boot-up" style={{ animationDelay: '280ms' }}>
        {tab === 'results' && <FindingsList detail={detail} />}
        {tab === 'timeline' && (
          <Timeline detail={detail} showAllEvents={showAllEvents} onToggleAllEvents={onToggleAllEvents} />
        )}
        {tab === 'rounds' && <RoundsList detail={detail} />}
        {tab === 'artifacts' && <ArtifactsList detail={detail} />}
      </div>
    </div>
  )
}
