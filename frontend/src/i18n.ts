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
    noneMatchingFilter: 'No findings match this filter.',
    showAll: 'Show all',
    filter: {
      open: 'Open',
      all: 'All',
      new: 'New',
      persistent: 'Persistent',
      likely_fixed: 'Likely fixed',
      unverified: 'Unverified',
      resolved: 'Resolved',
      dismissed: 'Dismissed',
    },
    status: {
      new: 'New',
      persistent: 'Persistent',
      likely_fixed: 'Likely fixed',
      unverified: 'Unverified',
      resolved: 'Resolved',
      dismissed: 'Dismissed',
    },
    roundSpan: (first: number, last: number) => (first === last ? `R${first}` : `R${first}–R${last}`),
    locationChanged: 'Location changed',
    previousLocation: (loc: string) => `Was ${loc}`,
    reopened: 'Reopened',
    mentionedAfterDismiss: (round: number) => `Still mentioned in Round ${round} (dismissed)`,
    likelyFixedHint: 'Code around this line changed and the latest round did not report it. Verify before marking resolved.',
    markResolved: 'Mark resolved',
    dismiss: 'Dismiss',
    undo: 'Undo',
    copyFixPrompt: 'Copy fix prompt',
    copiedFixPrompt: 'Fix prompt copied to clipboard.',
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
    parseMode: 'Parser',
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
    updateFinding: "Couldn't update the finding status",
    copyFailed: "Couldn't copy to clipboard",
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
  workbench: {
    allRepos: 'All repositories',
    filterAll: 'All',
    filterAttention: 'Needs attention',
    filterRunning: 'Running',
    agentsFooter: (n: number, agent: string) =>
      `${plural(n, 'agent', 'agents')} detected · default ${agent}`,
    noAgentsFooter: 'No agents detected',
    welcomeTagline: 'Review your changes with the coding agent you already use. Locally, before you push.',
    noAgentsTitle: 'No coding agent CLI detected',
    noAgentsBody: 'Install any supported CLI on your PATH, then reopen Settings to verify.',
    openModelSelection: 'Open Model selection',
    sandboxReadOnly: 'Read-only sandbox',
    export: 'Export',
    exportDiff: 'Export diff',
    deleteReview: 'Delete',
    confirmDelete: 'Confirm delete',
    deletedReview: 'Review deleted.',
    deleteFailed: "Couldn't delete the review",
    runRoundN: (n: number) => `Run round ${n}`,
    runRoundWith: (n: number) => `Run round ${n} with`,
    runRoundTooltip: 'Configure agent, model, reasoning, note, or focus for this round',
    focusOverride: 'Focus override (optional)',
    focusOverridePlaceholder: 'Override review focus for this round',
    higherIsSlower: 'Higher reasoning is slower and uses more quota.',
    levelsCount: (n: number, def: string | null) =>
      def ? `${plural(n, 'level', 'levels')} · model default ${def}` : plural(n, 'level', 'levels'),
    filesChanged: (n: number) => plural(n, 'file', 'files'),
    openCount: (n: number) => `${n} open`,
    likelyFixedCount: (n: number) => `${n} likely fixed`,
    roundRunningTitle: (n: number) => `Round ${n} is running`,
    stepCheckAgent: 'Check agent',
    stepPrepareContext: 'Prepare context',
    stepReview: 'Review',
    stepReadResults: 'Read results',
    stepFindingsVerdict: 'Findings and verdict',
    tabFindings: 'Findings',
    tabActivity: 'Activity',
    tabChanges: 'Changes',
    tabChecks: 'Checks',
    tabArtifacts: 'Artifacts',
    allSeverities: 'All severities',
    openInEditor: 'Open in editor',
    openedInEditor: (editor: string, loc: string) => `Opened ${loc} in ${editor}.`,
    openInEditorFailed: "Couldn't open file in editor",
    likelyFixedLegend:
      'Likely fixed: the agent did not report it again and the code at that location changed. Only you can mark a finding resolved.',
    retryRound: 'Retry round',
    retryModelDefault: 'Retry with Model default',
    failedAtStage: (stage: string) => `Stopped during ${stage}`,
    changesEmpty: 'No saved diff is available for this review yet.',
    changesTruncated: 'Diff output truncated for preview.',
    checksHeading: 'Validation checks',
    runChecksStandard: 'Run checks',
    runChecksFull: 'Run full checks',
    checksEmpty: 'No validation checks have run for this review yet.',
    checksStarted: 'Validation checks started.',
    checksFailed: "Couldn't start validation checks",
    rawJson: 'Raw JSON',
    live: 'live',
    createAndStart: 'Create and start',
    createOnly: 'Create',
    recentRepos: 'Recent',
    repoStatusGit: (branch: string, changed: number) =>
      `Git repository on ${branch} · ${plural(changed, 'changed file', 'changed files')}`,
    repoStatusNotGit: 'Directory exists, but it is not a git repository.',
    repoStatusMissing: 'Repository path does not exist on this machine.',
    ghAuthOk: 'GitHub CLI (gh) is authenticated.',
    ghAuthMissing: 'GitHub CLI (gh) is not authenticated; public PRs still work if GITHUB_TOKEN is set.',
    snapshotNote:
      'Mission Control copies your working tree (staged, unstaged and untracked files) into a temporary commit and runs the agent in an isolated worktree. Your files, git index and HEAD are not touched.',
    modalLede: 'Review changes with the coding agent you already use.',
    focusSecurity: 'Security',
    focusCorrectness: 'Correctness',
    focusPerformance: 'Performance',
    focusTests: 'Tests',
    settingsNavModel: 'Model selection',
    settingsNavStorage: 'Storage',
    settingsNavAbout: 'About',
    agentsOnMachine: 'Agents on this machine',
    detectAgain: 'Detect again',
    installed: 'Installed',
    notInstalled: 'Not installed',
    defaultBadge: 'Default',
    notSupported: 'Not supported',
    effectiveSettings: 'Effective settings',
    effectiveHierarchy: 'Highest layer wins: round, review, repository, global, built-in default',
    unsaved: 'Unsaved',
    discard: 'Discard',
    unsavedSummary: (agent: string) => `Unsaved changes for ${agent}`,
    storageTitle: 'Storage',
    storageLede: 'Mission Control stores its database, artifacts, and isolated worktrees in your platform application data directory.',
    storageDataDir: 'Data directory',
    storageDbPath: 'Database',
    storageWorktrees: 'Worktrees',
    storageConfigPath: 'Global config',
    storageBundledUi: 'Bundled Web UI',
    aboutTitle: 'About Mission Control',
    aboutBody: 'Local-first AI code review console driving the coding agent CLIs installed on your machine.',
    aboutVersion: 'Version',
    yes: 'Available',
    no: 'Not bundled',
    outputLanguage: 'Output language',
    idleTimeout: 'Idle timeout',
    builtInDefault: 'Built-in default',
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
    noneMatchingFilter: '当前筛选下没有问题。',
    showAll: '查看全部',
    filter: {
      open: '待处理',
      all: '全部',
      new: '新增',
      persistent: '持续',
      likely_fixed: '可能已修复',
      unverified: '未验证',
      resolved: '已解决',
      dismissed: '已忽略',
    },
    status: {
      new: '新增',
      persistent: '持续',
      likely_fixed: '可能已修复',
      unverified: '未验证',
      resolved: '已解决',
      dismissed: '已忽略',
    },
    roundSpan: (first, last) => (first === last ? `第 ${first} 轮` : `第 ${first}–${last} 轮`),
    locationChanged: '位置已变动',
    previousLocation: (loc) => `原位置 ${loc}`,
    reopened: '重新出现',
    mentionedAfterDismiss: (round) => `第 ${round} 轮仍提及（已忽略）`,
    likelyFixedHint: '该位置代码已修改且最新一轮未再报出，请确认后再标为已解决。',
    markResolved: '标为已解决',
    dismiss: '标为误报',
    undo: '撤回',
    copyFixPrompt: '复制修复提示',
    copiedFixPrompt: '已复制修复提示到剪贴板。',
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
    parseMode: '解析方式',
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
    updateFinding: '更新问题状态失败',
    copyFailed: '复制到剪贴板失败',
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
  workbench: {
    allRepos: '全部仓库',
    filterAll: '全部',
    filterAttention: '需要关注',
    filterRunning: '进行中',
    agentsFooter: (n, agent) => `检测到 ${n} 个 Agent · 默认 ${agent}`,
    noAgentsFooter: '未检测到 Agent',
    welcomeTagline: '用你本机已有的 Coding Agent 审查代码改动，在推送之前本地把关。',
    noAgentsTitle: '未检测到可用的 Agent CLI',
    noAgentsBody: '请先安装任一支持的 CLI 并确认其在 PATH 中，然后重新打开模型选择页验证。',
    openModelSelection: '打开模型选择',
    sandboxReadOnly: '只读沙箱',
    export: '导出',
    exportDiff: '导出 Diff',
    deleteReview: '删除',
    confirmDelete: '确认删除',
    deletedReview: '已删除审查任务。',
    deleteFailed: '删除审查任务失败',
    runRoundN: (n) => `运行第 ${n} 轮`,
    runRoundWith: (n) => `第 ${n} 轮运行设置`,
    runRoundTooltip: '为本轮选择 Agent、模型、推理强度、附言或关注点',
    focusOverride: '关注点覆盖（可选）',
    focusOverridePlaceholder: '仅本轮覆盖审查关注点',
    higherIsSlower: '更高的推理强度会更慢且消耗更多额度。',
    levelsCount: (n, def) => (def ? `${n} 个档位 · 模型默认 ${def}` : `${n} 个档位`),
    filesChanged: (n) => `${n} 个文件`,
    openCount: (n) => `${n} 条待处理`,
    likelyFixedCount: (n) => `${n} 条可能已修复`,
    roundRunningTitle: (n) => `第 ${n} 轮正在运行`,
    stepCheckAgent: '检查 Agent',
    stepPrepareContext: '准备上下文',
    stepReview: '执行审查',
    stepReadResults: '读取结果',
    stepFindingsVerdict: '提取问题与结论',
    tabFindings: '问题',
    tabActivity: '活动流',
    tabChanges: '改动',
    tabChecks: '验证检查',
    tabArtifacts: '产物',
    allSeverities: '全部严重程度',
    openInEditor: '在编辑器中打开',
    openedInEditor: (editor, loc) => `已在 ${editor} 中打开 ${loc}。`,
    openInEditorFailed: '无法在编辑器中打开文件',
    likelyFixedLegend: '可能已修复：最新一轮未再报出且该位置代码已变动；仅用户可将问题标为已解决。',
    retryRound: '重试本轮',
    retryModelDefault: '改用模型默认重试',
    failedAtStage: (stage) => `在「${stage}」阶段中止`,
    changesEmpty: '此审查暂无可预览的 Diff。',
    changesTruncated: 'Diff 内容过长，预览已截断。',
    checksHeading: '验证检查',
    runChecksStandard: '运行检查',
    runChecksFull: '运行完整检查',
    checksEmpty: '此审查尚未运行验证检查。',
    checksStarted: '已启动验证检查。',
    checksFailed: '启动验证检查失败',
    rawJson: '原始 JSON',
    live: '实时',
    createAndStart: '创建并开始',
    createOnly: '仅创建',
    recentRepos: '最近仓库',
    repoStatusGit: (branch, changed) => `Git 仓库（${branch}）· ${changed} 个变更文件`,
    repoStatusNotGit: '目录存在，但不是 Git 仓库。',
    repoStatusMissing: '本机找不到该仓库路径。',
    ghAuthOk: 'GitHub CLI (gh) 已登录。',
    ghAuthMissing: 'GitHub CLI (gh) 未登录；若配置了 GITHUB_TOKEN 仍可拉取公开 PR。',
    snapshotNote:
      'Mission Control 会将当前工作区（已暂存、未暂存与未跟踪文件）拍成临时快照并在隔离 worktree 中运行 Agent，不触碰你的文件、git index 与 HEAD。',
    modalLede: '用你本机已有的 Coding Agent 审查代码改动。',
    focusSecurity: '安全性',
    focusCorrectness: '正确性',
    focusPerformance: '性能',
    focusTests: '测试',
    settingsNavModel: '模型选择',
    settingsNavStorage: '存储路径',
    settingsNavAbout: '关于',
    agentsOnMachine: '本机检测到的 Agent',
    detectAgain: '重新检测',
    installed: '已安装',
    notInstalled: '未安装',
    defaultBadge: '默认',
    notSupported: '不支持',
    effectiveSettings: '生效设置',
    effectiveHierarchy: '优先级从高到低：本轮、审查默认、仓库配置、全局配置、内置默认',
    unsaved: '未保存',
    discard: '放弃修改',
    unsavedSummary: (agent) => `${agent} 有未保存的更改`,
    storageTitle: '存储路径',
    storageLede: 'Mission Control 将数据库、产物与隔离 worktree 保存在系统应用数据目录中。',
    storageDataDir: '数据目录',
    storageDbPath: '数据库文件',
    storageWorktrees: 'Worktree 目录',
    storageConfigPath: '全局配置文件',
    storageBundledUi: '内置前端静态包',
    aboutTitle: '关于 Mission Control',
    aboutBody: '本地优先的 AI 代码审查控制台，驱动你本机已安装的 Coding Agent CLI。',
    aboutVersion: '版本',
    yes: '已内置',
    no: '未内置',
    outputLanguage: '输出语言',
    idleTimeout: '空闲超时',
    builtInDefault: '内置默认',
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
