export type Language = 'en' | 'zh'

type Translation = {
  // Common
  loading: string
  settings: string
  back: string
  refresh: string

  // Task list
  newTask: string
  noTasks: string

  // Task status
  status: {
    created: string
    context_ready: string
    ingesting: string
    running: string
    waiting_human: string
    validating: string
    completed: string
    failed: string
    aborted: string
  }

  // Stage labels
  stage: {
    created: string
    review_ready: string
    review_prepare_failed: string
    review_round_running: string
    review_round_failed: string
    aborted: string
    validating: string
  }

  // Review source
  source: {
    pull_request: string
    local_diff: string
  }

  // Actions
  actions: {
    startReview: string
    abort: string
    exportSummary: string
    delete: string
  }

  // Detail tabs
  tabs: {
    results: string
    timeline: string
    rounds: string
    artifacts: string
  }

  // Results
  results: {
    noResult: string
    noFindings: string
    noFindingsThisRound: string
    noResultYet: string
  }

  // Verdict
  verdict: {
    clear: string
    concerns: string
    failed: string
    inconclusive: string
  }

  // Events
  events: {
    all: (count: number) => string
    important: (count: number) => string
    showAll: (count: number) => string
    showImportant: string
    noEvents: string
    hidden: (count: number) => string
  }

  // Artifacts
  artifacts: {
    noArtifacts: string
  }

  // Metadata
  meta: {
    backend: string
    round: string
    duration: string
    exitCode: string
  }

  // Time
  time: {
    secondsAgo: (n: number) => string
    minutesAgo: (n: number) => string
    hoursAgo: (n: number) => string
    daysAgo: (n: number) => string
  }

  // Errors
  errors: {
    loadTasksFailed: string
    loadDetailFailed: string
    createTaskFailed: string
    startReviewFailed: string
    abortFailed: string
    exportFailed: string
  }

  // Success messages
  success: {
    exported: (path: string) => string
  }

  // Review mode descriptions
  reviewMode: {
    pr: string
    local: string
    sourceLabel: string
  }
}

const en: Translation = {
  loading: 'Loading...',
  settings: 'Settings',
  back: 'Back',
  refresh: 'Refresh',

  newTask: 'New Task',
  noTasks: 'No tasks yet',

  status: {
    created: 'Created',
    context_ready: 'Ready',
    ingesting: 'Preparing',
    running: 'Running',
    waiting_human: 'Waiting',
    validating: 'Validating',
    completed: 'Completed',
    failed: 'Failed',
    aborted: 'Aborted',
  },

  stage: {
    created: 'Created',
    review_ready: 'Ready for review',
    review_prepare_failed: 'Review prep failed',
    review_round_running: 'Review running',
    review_round_failed: 'Review failed',
    aborted: 'Aborted',
    validating: 'Validating',
  },

  source: {
    pull_request: 'PR',
    local_diff: 'Local',
  },

  actions: {
    startReview: 'Start Review',
    abort: 'Abort',
    exportSummary: 'Export Summary',
    delete: 'Delete',
  },

  tabs: {
    results: 'Results',
    timeline: 'Timeline',
    rounds: 'Rounds',
    artifacts: 'Artifacts',
  },

  results: {
    noResult: 'No result',
    noFindings: 'No findings',
    noFindingsThisRound: 'This round produced no structured findings.',
    noResultYet: 'No review results yet. Run a round first.',
  },

  verdict: {
    clear: 'Clear',
    concerns: 'Concerns',
    failed: 'Failed',
    inconclusive: 'Inconclusive',
  },

  events: {
    all: (count: number) => `All ${count} events`,
    important: (count: number) => `${count} important events`,
    showAll: (count: number) => `Show all ${count}`,
    showImportant: 'Show important only',
    noEvents: 'No events yet',
    hidden: (count: number) => `${count} hidden`,
  },

  artifacts: {
    noArtifacts: 'No artifact files',
  },

  meta: {
    backend: 'Backend',
    round: 'Round',
    duration: 'Duration',
    exitCode: 'Exit code',
  },

  time: {
    secondsAgo: (n) => `${n}s ago`,
    minutesAgo: (n) => `${n}m ago`,
    hoursAgo: (n) => `${n}h ago`,
    daysAgo: (n) => `${n}d ago`,
  },

  errors: {
    loadTasksFailed: 'Failed to load tasks',
    loadDetailFailed: 'Failed to load task details',
    createTaskFailed: 'Failed to create review task',
    startReviewFailed: 'Failed to start review',
    abortFailed: 'Failed to abort review',
    exportFailed: 'Failed to export summary',
  },

  success: {
    exported: (path) => `Summary exported to ${path}`,
  },

  reviewMode: {
    pr: 'Fetch GitHub PR diff and discussion, compile context, and send to AI backend for review.',
    local: 'Review uncommitted changes in your working directory.',
    sourceLabel: 'Review source',
  },
}

const zh: Translation = {
  loading: '加载中...',
  settings: '设置',
  back: '返回',
  refresh: '刷新',

  newTask: '新建任务',
  noTasks: '暂无任务',

  status: {
    created: '已创建',
    context_ready: '待审查',
    ingesting: '准备中',
    running: '审查中',
    waiting_human: '待处理',
    validating: '验证中',
    completed: '已完成',
    failed: '失败',
    aborted: '已中止',
  },

  stage: {
    created: '已创建',
    review_ready: '待审查',
    review_prepare_failed: '审查失败',
    review_round_running: '审查中',
    review_round_failed: '审查失败',
    aborted: '已中止',
    validating: '验证中',
  },

  source: {
    pull_request: 'PR',
    local_diff: '本地',
  },

  actions: {
    startReview: '启动审查',
    abort: '中止',
    exportSummary: '导出摘要',
    delete: '删除',
  },

  tabs: {
    results: '审查结果',
    timeline: '时间线',
    rounds: '审查轮次',
    artifacts: '工件',
  },

  results: {
    noResult: '暂无结果',
    noFindings: '无问题',
    noFindingsThisRound: '这一轮没有产出结构化问题。',
    noResultYet: '还没有审查结果，先跑一轮吧。',
  },

  verdict: {
    clear: '通过',
    concerns: '关注',
    failed: '阻止',
    inconclusive: '不确定',
  },

  events: {
    all: (count) => `全部 ${count} 条事件`,
    important: (count) => `${count} 条重要事件`,
    showAll: (count) => `显示全部 ${count} 条`,
    showImportant: '仅看重要事件',
    noEvents: '暂无事件',
    hidden: (count) => `已隐藏 ${count} 条`,
  },

  artifacts: {
    noArtifacts: '暂无产物文件',
  },

  meta: {
    backend: '后端',
    round: '轮次',
    duration: '时长',
    exitCode: '退出码',
  },

  time: {
    secondsAgo: (n) => `${n} 秒前`,
    minutesAgo: (n) => `${n} 分钟前`,
    hoursAgo: (n) => `${n} 小时前`,
    daysAgo: (n) => `${n} 天前`,
  },

  errors: {
    loadTasksFailed: '任务列表加载失败',
    loadDetailFailed: '任务详情加载失败',
    createTaskFailed: '创建审查任务失败',
    startReviewFailed: '启动审查失败',
    abortFailed: '中止审查失败',
    exportFailed: '导出摘要失败',
  },

  success: {
    exported: (path) => `摘要已导出到 ${path}`,
  },

  reviewMode: {
    pr: '拉取 GitHub PR 的 diff 与讨论，编译上下文后交给 AI 后端审查。',
    local: '审查工作目录中未提交的改动。',
    sourceLabel: '审查来源',
  },
}

export const translations: Record<Language, Translation> = { en, zh }

const LANG_KEY = 'amc-language'

export const getStoredLanguage = (): Language => {
  const stored = localStorage.getItem(LANG_KEY) as Language | null
  return stored === 'zh' || stored === 'en' ? stored : 'en'
}

export const setStoredLanguage = (lang: Language) => {
  localStorage.setItem(LANG_KEY, lang)
}
