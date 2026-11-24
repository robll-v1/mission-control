/**
 * Interface text for the web console, in English and Simplified Chinese.
 *
 * `en` is the source of truth: its shape is the `Messages` type, so a key that
 * is missing from, or extra in, another language fails `tsc`. The same rule is
 * checked at runtime by tests/i18n.test.ts.
 *
 * Terms follow the glossary in docs/design/redesign-draft.md, section 5.
 *
 * This module imports nothing and touches no browser global at import time,
 * so `node --test` can load it as is.
 */

export type Language = 'en' | 'zh-CN'

export const DEFAULT_LANGUAGE: Language = 'en'

/** localStorage key for the interface language the user picked. */
export const LANGUAGE_STORAGE_KEY = 'amc_ui_language'

/** Options for the language switch, each named in its own language. */
export const LANGUAGES: readonly { code: Language; label: string; name: string }[] = [
  { code: 'en', label: 'EN', name: 'English' },
  { code: 'zh-CN', label: '中文', name: '简体中文' },
]

/**
 * Language the agent writes the review in. It is a separate setting from the
 * interface language: switching the interface to Chinese does not change it.
 * Values follow the backend's vocabulary (`ExecutionService.start_task`),
 * which is 'en' | 'zh' rather than a BCP 47 tag.
 */
export type ReviewLanguage = 'en' | 'zh'

export const DEFAULT_REVIEW_LANGUAGE: ReviewLanguage = 'en'

const plural = (n: number, one: string, other: string) => `${n} ${n === 1 ? one : other}`

const en = {
  common: {
    dismiss: 'Dismiss',
    cancel: 'Cancel',
    back: 'Back',
  },
  topbar: {
    tagline: 'AI code review console',
    toggleReviewList: 'Show or hide reviews',
    modelSelection: 'Model selection',
    /** Tooltip of the language switch, written in the language it switches to. */
    switchToThisLanguage: 'Switch to English',
    switchToLight: 'Switch to light theme',
    switchToDark: 'Switch to dark theme',
    lightTheme: 'Light theme',
    darkTheme: 'Dark theme',
    newReview: 'New review',
  },
  status: {
    created: 'Created',
    context_ready: 'Ready',
    ingesting: 'Preparing',
    running: 'Reviewing',
    waiting_human: 'Waiting for you',
    validating: 'Validating',
    completed: 'Completed',
    failed: 'Failed',
    aborted: 'Cancelled',
  },
  stage: {
    created: 'Created',
    review_ready: 'Ready for review',
    preflight: 'Checking agent',
    preflight_failed: 'Agent check failed',
    prepare_review: 'Preparing context',
    review_prepare_failed: 'Preparation failed',
    review_in_progress: 'Review in progress',
    awaiting_next_round: 'Awaiting next round',
    review_failed: 'Review failed',
    review_done: 'Review complete',
    interrupted: 'Interrupted',
    aborted: 'Cancelled',
    validate: 'Validating',
    handoff: 'Awaiting handoff',
  },
  verdict: {
    clear: 'No issues found',
    concerns: 'Needs attention',
    failed: 'Review failed',
    inconclusive: 'Inconclusive',
  },
  severity: {
    critical: 'Critical',
    high: 'High',
    medium: 'Medium',
    low: 'Low',
  },
  time: {
    secondsAgo: (n: number) => `${n}s ago`,
    minutesAgo: (n: number) => `${n}m ago`,
    hoursAgo: (n: number) => `${n}h ago`,
    daysAgo: (n: number) => `${n}d ago`,
  },
  counts: {
    findings: (n: number) => plural(n, 'finding', 'findings'),
    /** Where the label already says what is counted; Chinese drops the noun. */
    findingsCompact: (n: number) => plural(n, 'finding', 'findings'),
  },
  source: {
    localChanges: (base: string | null) =>
      base ? `Local changes vs ${base}` : 'Local changes · base auto-detected',
  },
  sidebar: {
    heading: 'Reviews',
    notRun: 'Not run',
    empty: 'No reviews yet',
  },
  empty: {
    title: 'Select a review',
    body: 'Pick a review from the list to see its verdict and findings, or create a new one.',
  },
  detail: {
    runFirstRound: 'Run first round',
    runNextRound: 'Run next round',
    exportSummary: 'Export summary',
    cancelRound: 'Cancel round',
    repository: 'Repository',
    agent: 'Agent',
    rounds: 'Rounds',
    commit: 'Commit',
    updated: 'Updated',
    background: 'Background',
    roundSettings: 'This round',
    roundAgent: 'Agent',
    roundModel: 'Model',
    roundReasoning: 'Reasoning',
    roundLanguage: 'Output language',
    roundNote: 'Note for this round (optional)',
    roundNotePlaceholder: 'Extra context or what changed since last round',
    reviewLangEn: 'English (en)',
    reviewLangZh: 'Chinese (zh)',
  },
  result: {
    none: 'No result yet',
    noSummary: 'This review has no structured verdict yet.',
  },
  tabs: {
    results: 'Findings',
    timeline: 'Activity',
    rounds: 'Rounds',
    artifacts: 'Artifacts',
  },
  findings: {
    heading: 'Findings',
    noLocation: 'No location',
    noneThisRound: 'This round produced no structured findings.',
    noResultYet: 'No results yet. Run a round first.',
  },
  timeline: {
    allEvents: (n: number) => `All ${plural(n, 'event', 'events')}`,
    importantEvents: (n: number) => plural(n, 'important event', 'important events'),
    showAll: (n: number) => `Show all ${plural(n, 'event', 'events')}`,
    importantOnly: 'Important only',
    empty: 'No events yet',
    fullOutput: 'Show full output',
  },
  rounds: {
    title: (n: number) => `Round ${n}`,
    agent: 'Agent',
    model: 'Model',
    reasoning: 'Reasoning',
    language: 'Language',
    status: 'Status',
    duration: 'Duration',
    exitCode: 'Exit code',
    commit: 'Commit',
    startedAt: 'Started',
    empty: 'No rounds yet',
  },
  artifacts: {
    empty: 'No artifacts yet',
  },
  newReview: {
    title: 'New review',
    hintPullRequest:
      'Fetches the pull request diff and discussion from GitHub, builds the review context, and hands it to your agent.',
    hintLocal:
      "Reviews a local repository's commits, staged changes and working-tree changes against its base branch.",
    source: 'What to review',
    pullRequest: 'Pull request',
    localChanges: 'Local changes',
    repoPath: 'Repository path',
    prUrl: 'Pull request URL',
    baseBranch: 'Base branch (optional)',
    baseBranchPlaceholder: 'Auto-detects main or master',
    focus: 'Review focus (optional)',
    focusPlaceholder: 'Risks, modules, compatibility or test gaps to focus on',
    agent: 'Agent',
    agentAuto: 'Auto (.amc.yaml / global default)',
    model: 'Model',
    reasoning: 'Reasoning',
    language: 'Output language',
    creating: 'Creating…',
    create: 'Create review',
  },
  errors: {
    missingRepoAndPr: 'Enter a repository path and a pull request URL.',
    missingRepo: 'Enter a repository path.',
    loadReviews: "Couldn't load the review list",
    loadReview: "Couldn't load the review",
    createReview: "Couldn't create the review",
    startRound: "Couldn't start the round",
    cancelRound: "Couldn't cancel the round",
    exportSummary: "Couldn't export the summary",
  },
  notices: {
    exported: (path: string) => `Summary exported to ${path}`,
  },
  settings: {
    backToReviews: 'Back to reviews',
    eyebrow: 'Settings',
    title: 'Model selection',
    // The lede and the install hint wrap a <code> element, so each is split
    // around it.
    ledeBeforeFile:
      "Mission Control runs reviews through the agent CLIs installed on this machine. Choose the default agent and model here. Reviews started in the web UI use this configuration, but a repository's ",
    ledeAfterFile: ' still takes precedence.',
    agentHeading: 'Agent',
    detecting: 'Detecting agent CLIs on this machine…',
    detected: (n: number) => `${n} detected`,
    noneDetected: 'No agent CLI detected',
    installBeforePath: 'Install any one of these and make sure it is on your ',
    installAfterPath: ', then reopen this page.',
    command: (command: string) => `Command: ${command}`,
    current: 'Current',
    modelsFound: (n: number) => `${plural(n, 'model', 'models')} found`,
    usesAgentDefault: "Uses the CLI's default model",
    versionUnknown: 'Version unknown',
    savedAgentMissing: (name: string) =>
      `The saved agent, ${name}, was not detected. Install it, or choose another agent above.`,
    modelHeading: 'Model',
    showingModels: (shown: number, total: number) =>
      `Showing ${shown} of ${total}; preview and deprecated models are hidden`,
    modelSource: {
      cli: 'The model list comes from the local CLI.',
      catalog_cache: 'The model list comes from the local model catalog cache.',
      cache: 'The model list comes from a local cache.',
      config: 'The model list comes from local configuration.',
      unknown: 'The source of the model list is unknown.',
      unreadable: "Couldn't read a model list. You can still use the agent default or keep the configured model.",
    },
    notCallable: 'A listed model is not proof that your account can call it; only a real request can tell.',
    searchModels: 'Search models',
    agentDefault: 'CLI default',
    agentDefaultHint: 'No model is set; the agent CLI picks one',
    noModelList: 'This agent has no model list that can be read',
    customModelHint: 'Current configuration, not in the detected list',
    noMatch: (query: string) => `No models match "${query}"`,
    willSave: 'Will save as',
    currentConfig: 'Current configuration',
    saving: 'Saving…',
    save: 'Save',
    saved: (agent: string, model: string) => `Saved. New reviews will use ${agent} · ${model}.`,
    saveFailed: "Couldn't save. Try again in a moment.",
    loadFailed: "Couldn't read the configuration. Make sure the Mission Control server is running.",
    reasoningHeading: 'Reasoning effort',
    modelDefault: 'Model default',
    modelDefaultWithLevel: (level: string) => `Model default (${level})`,
    modelDefaultHint: 'Do not pass a reasoning flag; let the model use its default',
    reasoningUnavailable: 'Reasoning effort is not exposed by this agent CLI.',
    reasoningSource: {
      cache: 'Levels read from local model cache.',
      cli_help: 'Levels parsed from local CLI help.',
      config: 'Configured via legacy model#variant syntax when set.',
      unsupported: 'This CLI does not expose a reasoning effort option.',
      unknown: 'Could not read reasoning levels locally; CLI default will be used.',
    },
    effectiveLabel: 'Effective now',
    sourceLayer: {
      round: 'round',
      explicit: 'cli',
      review: 'review',
      env: 'env',
      repo: 'repo',
      global: 'global',
      default: 'default',
      unsupported: 'unsupported',
    },
    checkAgent: 'Check agent',
    checkingAgent: 'Checking…',
    checkPassed: 'Static check passed',
    checkFailed: 'Static check failed',
    checkError: "Couldn't run the static check.",
  },
}

export type Messages = typeof en

const zhCN: Messages = {
  common: {
    dismiss: '关闭提示',
    cancel: '取消',
    back: '返回',
  },
  topbar: {
    tagline: 'AI 代码审查控制台',
    toggleReviewList: '切换任务列表',
    modelSelection: '模型选择',
    switchToThisLanguage: '切换到中文',
    switchToLight: '切换到浅色主题',
    switchToDark: '切换到深色主题',
    lightTheme: '浅色主题',
    darkTheme: '深色主题',
    newReview: '新建审查',
  },
  status: {
    created: '已创建',
    context_ready: '待审查',
    ingesting: '准备中',
    running: '审查中',
    waiting_human: '待处理',
    validating: '验证中',
    completed: '已完成',
    failed: '失败',
    aborted: '已取消',
  },
  stage: {
    created: '已创建',
    review_ready: '待审查',
    preflight: '检查 Agent',
    preflight_failed: 'Agent 检查失败',
    prepare_review: '准备上下文',
    review_prepare_failed: '准备失败',
    review_in_progress: '审查进行中',
    awaiting_next_round: '等待下一轮',
    review_failed: '审查失败',
    review_done: '审查完成',
    interrupted: '被中断',
    aborted: '已取消',
    validate: '验证中',
    handoff: '待交接',
  },
  verdict: {
    clear: '未发现明显问题',
    concerns: '需要关注',
    failed: '审查失败',
    inconclusive: '结论不明确',
  },
  severity: {
    critical: '严重',
    high: '高',
    medium: '中',
    low: '低',
  },
  time: {
    secondsAgo: (n) => `${n} 秒前`,
    minutesAgo: (n) => `${n} 分钟前`,
    hoursAgo: (n) => `${n} 小时前`,
    daysAgo: (n) => `${n} 天前`,
  },
  counts: {
    findings: (n) => `${n} 条问题`,
    findingsCompact: (n) => `${n} 条`,
  },
  source: {
    localChanges: (base) => `本地变更 · ${base || '自动基准'}`,
  },
  sidebar: {
    heading: '审查任务',
    notRun: '未运行',
    empty: '还没有审查任务',
  },
  empty: {
    title: '选择一个审查任务',
    body: '从左侧列表挑一个任务查看结论与问题列表，或新建一次 PR 审查。',
  },
  detail: {
    runFirstRound: '开始首轮审查',
    runNextRound: '发起下一轮审查',
    exportSummary: '导出摘要',
    cancelRound: '取消本轮',
    repository: '仓库',
    agent: '后端',
    rounds: '轮次',
    commit: 'Commit',
    updated: '更新',
    background: '任务背景',
    roundSettings: '本轮设置',
    roundAgent: '后端',
    roundModel: '模型',
    roundReasoning: '推理强度',
    roundLanguage: '输出语言',
    roundNote: '本轮附言（可选）',
    roundNotePlaceholder: '补充背景或说明相对上一轮做了哪些修改',
    reviewLangEn: '英文 (en)',
    reviewLangZh: '中文 (zh)',
  },
  result: {
    none: '暂无结果',
    noSummary: '这个任务还没有产出结构化的审查结论。',
  },
  tabs: {
    results: '审查结果',
    timeline: '事件流',
    rounds: '审查轮次',
    artifacts: '产物',
  },
  findings: {
    heading: '问题列表',
    noLocation: '未指明位置',
    noneThisRound: '这一轮没有产出结构化问题。',
    noResultYet: '还没有审查结果，先跑一轮吧。',
  },
  timeline: {
    allEvents: (n) => `全部 ${n} 条事件`,
    importantEvents: (n) => `${n} 条重要事件`,
    showAll: (n) => `显示全部 ${n} 条`,
    importantOnly: '仅看重要事件',
    empty: '暂无事件',
    fullOutput: '查看完整输出',
  },
  rounds: {
    title: (n) => `第 ${n} 轮`,
    agent: '后端',
    model: '模型',
    reasoning: '推理强度',
    language: '语言',
    status: '状态',
    duration: '耗时',
    exitCode: '退出码',
    commit: 'Commit',
    startedAt: '开始于',
    empty: '还没有跑过任何一轮审查',
  },
  artifacts: {
    empty: '暂无产物文件',
  },
  newReview: {
    title: '新建代码审查',
    hintPullRequest: '拉取 GitHub PR 的 diff 与讨论，编译上下文后交给 AI 后端审查。',
    hintLocal: '审查本地仓库相对基准分支的提交、暂存区和工作区变更。',
    source: '审查来源',
    pullRequest: 'GitHub PR',
    localChanges: '本地变更',
    repoPath: '本地仓库路径',
    prUrl: 'GitHub PR 链接',
    baseBranch: '基准分支（可选）',
    baseBranchPlaceholder: '自动检测 main 或 master',
    focus: '审查重点（可选）',
    focusPlaceholder: '想让审查特别关注的风险点、模块、兼容性或测试缺口',
    agent: '后端',
    agentAuto: '自动（遵循 .amc.yaml / 全局默认）',
    model: '模型',
    reasoning: '推理强度',
    language: '输出语言',
    creating: '正在创建…',
    create: '创建审查',
  },
  errors: {
    missingRepoAndPr: '请填写仓库路径和 GitHub PR 链接。',
    missingRepo: '请填写仓库路径。',
    loadReviews: '任务列表加载失败',
    loadReview: '任务详情加载失败',
    createReview: '创建审查任务失败',
    startRound: '启动审查失败',
    cancelRound: '取消本轮失败',
    exportSummary: '导出摘要失败',
  },
  notices: {
    exported: (path) => `摘要已导出到 ${path}`,
  },
  settings: {
    backToReviews: '返回审查',
    eyebrow: '设置',
    title: '模型选择',
    ledeBeforeFile:
      'Mission Control 通过你本机已安装的 Agent CLI 执行审查。在这里选择默认使用的后端和模型，网页中发起的审查都会使用这份配置；仓库内的',
    ledeAfterFile: ' 仍然优先。',
    agentHeading: '后端',
    detecting: '正在检测本机的 Agent CLI…',
    detected: (n) => `检测到 ${n} 个`,
    noneDetected: '没有检测到可用的 Agent CLI',
    installBeforePath: '安装下面任意一个，并确认它在 ',
    installAfterPath: ' 中，然后重新打开此页面。',
    command: (command) => `命令：${command}`,
    current: '当前',
    modelsFound: (n) => `已发现 ${n} 个模型`,
    usesAgentDefault: '使用后端默认模型',
    versionUnknown: '版本未知',
    savedAgentMissing: (name) => `已保存的后端「${name}」当前没有检测到。请先安装它，或在上方改选其他后端。`,
    modelHeading: '模型',
    showingModels: (shown, total) => `显示 ${shown} / ${total}，已隐藏测试版和弃用模型`,
    modelSource: {
      cli: '模型列表来自本机 CLI。',
      catalog_cache: '模型列表来自本机模型目录缓存。',
      cache: '模型列表来自本机缓存。',
      config: '模型列表来自本机配置。',
      unknown: '模型列表来源未知。',
      unreadable: '未能读取模型列表；仍可选择后端默认模型或保留已配置的模型。',
    },
    notCallable: '列表不代表账号调用权限，能否调用请以实际请求结果为准。',
    searchModels: '搜索模型',
    agentDefault: '后端默认模型',
    agentDefaultHint: '不指定，由后端自己决定',
    noModelList: '这个后端没有提供可读取的模型列表',
    customModelHint: '当前配置，不在检测列表中',
    noMatch: (query) => `没有匹配「${query}」的模型`,
    willSave: '将保存为',
    currentConfig: '当前配置',
    saving: '保存中…',
    save: '保存配置',
    saved: (agent, model) => `已保存。之后发起的审查会使用 ${agent} · ${model}。`,
    saveFailed: '保存失败，请稍后重试',
    loadFailed: '无法读取配置，请确认后端服务正在运行',
    reasoningHeading: '推理强度',
    modelDefault: '模型默认',
    modelDefaultWithLevel: (level) => `模型默认 (${level})`,
    modelDefaultHint: '不传推理参数，使用模型默认强度',
    reasoningUnavailable: '当前后端 CLI 未开放推理强度选项。',
    reasoningSource: {
      cache: '档位读取自本机模型缓存。',
      cli_help: '档位解析自本机 CLI 帮助信息。',
      config: '配置了旧版 model#variant 语法时沿用。',
      unsupported: '此 CLI 不支持推理强度参数。',
      unknown: '未能从本机读取档位列表，将使用 CLI 默认值。',
    },
    effectiveLabel: '当前生效',
    sourceLayer: {
      round: '本轮',
      explicit: '命令行',
      review: '任务',
      env: '环境变量',
      repo: '仓库配置',
      global: '全局配置',
      default: '默认',
      unsupported: '不支持',
    },
    checkAgent: '检查 Agent',
    checkingAgent: '检查中…',
    checkPassed: '静态检查通过',
    checkFailed: '静态检查失败',
    checkError: '无法执行静态检查。',
  },
}

export const messages: Readonly<Record<Language, Messages>> = { en, 'zh-CN': zhCN }

/**
 * Text resolved when it is rendered rather than when it is created, so a
 * notice already on screen follows a language switch, and callbacks that set
 * one do not need the current dictionary.
 */
export type Localized = (t: Messages) => string

/** Text that reads the same in every language, such as a server message. */
export const verbatim = (text: string): Localized => () => text

/** An error's own message, shown as sent; `fallback` when it has none. */
export const errorMessage = (err: unknown, fallback: Localized): Localized =>
  err instanceof Error && err.message ? verbatim(err.message) : fallback

/**
 * The label for a value the backend can extend, such as a stage name. A value
 * the dictionary does not know is shown as is; inherited keys like
 * "constructor" never resolve to a function.
 */
export const labelFor = (table: Readonly<Record<string, string>>, key: string): string =>
  Object.prototype.hasOwnProperty.call(table, key) ? table[key] : key

export const isLanguage = (value: unknown): value is Language =>
  LANGUAGES.some((option) => option.code === value)

/** The language the top-bar switch offers: the next one in LANGUAGES. */
export function otherLanguage(language: Language): (typeof LANGUAGES)[number] {
  const index = LANGUAGES.findIndex((option) => option.code === language)
  return LANGUAGES[(index + 1) % LANGUAGES.length]
}

type LanguageStorage = Pick<Storage, 'getItem' | 'setItem'>

// Reading window.localStorage throws where storage is blocked, for example
// with cookies disabled.
const browserStorage = (): LanguageStorage | null => {
  try {
    return typeof window === 'undefined' ? null : window.localStorage
  } catch {
    return null
  }
}

/** The language the user picked earlier, or English if they never did. */
export function readStoredLanguage(storage: LanguageStorage | null = browserStorage()): Language {
  try {
    const stored = storage?.getItem(LANGUAGE_STORAGE_KEY)
    return isLanguage(stored) ? stored : DEFAULT_LANGUAGE
  } catch {
    return DEFAULT_LANGUAGE
  }
}

/** Remember an explicit choice. Without storage it still applies until reload. */
export function storeLanguage(language: Language, storage: LanguageStorage | null = browserStorage()): void {
  try {
    storage?.setItem(LANGUAGE_STORAGE_KEY, language)
  } catch {
    // Quota or privacy settings; nothing else to do.
  }
}
