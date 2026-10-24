/* Snake_case → camelCase adapter for the task detail endpoint. */

import type { TaskDetail, TaskDetailResponse } from './types'

export const normalizeDetail = (payload: TaskDetailResponse): TaskDetail => {
  const events = payload.events
  const importantEvents = payload.important_events ?? events
  return {
    task: payload.task,
    events,
    importantEvents,
    eventStats: payload.event_stats ?? {
      all_count: events.length,
      important_count: importantEvents.length,
      hidden_count: Math.max(events.length - importantEvents.length, 0),
    },
    latestReviewResult: payload.latest_review_result ?? payload.task.latest_review_result ?? null,
    reviewRounds: payload.review_rounds ?? payload.runs ?? [],
    artifacts: payload.artifacts,
  }
}
