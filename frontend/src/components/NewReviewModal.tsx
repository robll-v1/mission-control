import { useEffect, useRef } from 'react'
import { CloseIcon } from './Icons'
import type { ReviewSource } from '../lib/types'

type NewReviewModalProps = {
  repoPath: string
  setRepoPath: (v: string) => void
  reviewSource: ReviewSource
  setReviewSource: (v: ReviewSource) => void
  prUrl: string
  setPrUrl: (v: string) => void
  baseBranch: string
  setBaseBranch: (v: string) => void
  reviewFocus: string
  setReviewFocus: (v: string) => void
  isCreating: boolean
  onCreate: () => void
  onClose: () => void
}

/* New-review dialog. A console "launch form": source mode switch,
   repo path, PR url or base branch, review focus. Escape closes;
   focus lands on the first field. */
export function NewReviewModal({
  repoPath,
  setRepoPath,
  reviewSource,
  setReviewSource,
  prUrl,
  setPrUrl,
  baseBranch,
  setBaseBranch,
  reviewFocus,
  setReviewFocus,
  isCreating,
  onCreate,
  onClose,
}: NewReviewModalProps) {
  const firstFieldRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    firstFieldRef.current?.focus()
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const canSubmit =
    !isCreating &&
    repoPath.trim() !== '' &&
    (reviewSource === 'local_diff' || prUrl.trim() !== '')

  return (
    <div className="overlay" onClick={onClose} role="presentation">
      <div
        className="dialog"
        onClick={(event) => event.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label="新建代码审查"
      >
        <span className="brackets" aria-hidden="true" />
        <h2>新建代码审查</h2>
        <p className="dialog-hint">
          {reviewSource === 'pull_request'
            ? '拉取 GitHub PR 的 diff 与讨论，编译上下文后交给 AI 后端审查。'
            : '审查本地仓库相对基准分支的提交、暂存区和工作区变更。'}
        </p>

        <div className="mode-switch" role="group" aria-label="审查来源">
          <button
            type="button"
            className={reviewSource === 'pull_request' ? 'active' : ''}
            aria-pressed={reviewSource === 'pull_request'}
            onClick={() => setReviewSource('pull_request')}
          >
            GitHub PR
          </button>
          <button
            type="button"
            className={reviewSource === 'local_diff' ? 'active' : ''}
            aria-pressed={reviewSource === 'local_diff'}
            onClick={() => setReviewSource('local_diff')}
          >
            本地变更
          </button>
        </div>

        <label className="field">
          <span className="field-label">本地仓库路径 / repo path</span>
          <input
            ref={firstFieldRef}
            value={repoPath}
            onChange={(event) => setRepoPath(event.target.value)}
            placeholder="~/code/your-repo"
          />
        </label>

        {reviewSource === 'pull_request' ? (
          <label className="field">
            <span className="field-label">GitHub PR 链接 / pr url</span>
            <input
              value={prUrl}
              onChange={(event) => setPrUrl(event.target.value)}
              placeholder="https://github.com/org/repo/pull/123"
            />
          </label>
        ) : (
          <label className="field">
            <span className="field-label">基准分支 / base branch（可选）</span>
            <input
              value={baseBranch}
              onChange={(event) => setBaseBranch(event.target.value)}
              placeholder="自动检测 main 或 master"
            />
          </label>
        )}

        <label className="field">
          <span className="field-label">审查重点 / focus（可选）</span>
          <textarea
            value={reviewFocus}
            onChange={(event) => setReviewFocus(event.target.value)}
            rows={3}
            placeholder="想让审查特别关注的风险点、模块、兼容性或测试缺口"
          />
        </label>

        <div className="dialog-actions">
          <button className="btn btn-ghost" disabled={isCreating} onClick={onClose}>
            <CloseIcon />
            取消
          </button>
          <button className="btn btn-primary" disabled={!canSubmit} onClick={onCreate}>
            {isCreating ? '正在创建…' : '创建审查'}
          </button>
        </div>
      </div>
    </div>
  )
}
