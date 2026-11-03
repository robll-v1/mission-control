/**
 * Tracks pending start requests per task, and the aborts aimed at each one.
 *
 * A start request stays pending through preflight and prepare, and an abort
 * accepted in that window makes it fail with "cancelled". That failure is
 * the expected result of the user's own action, so it must not be shown as
 * an error. Each start request keeps its own record: starting or aborting
 * another task, or restarting this one, never touches it, and the answer
 * does not depend on whether the start or the abort response arrives first.
 *
 * Kept free of React so its ordering rules can be tested directly.
 */
export type PendingStart = {
  readonly taskId: string
  /** One entry per abort sent while this start was pending: true if accepted. */
  readonly aborts: Promise<boolean>[]
}

export class StartTracker {
  private readonly pending = new Map<string, PendingStart>()

  begin(taskId: string): PendingStart {
    const request: PendingStart = { taskId, aborts: [] }
    this.pending.set(taskId, request)
    return request
  }

  /** Forget a finished request; a newer start of the same task is kept. */
  end(request: PendingStart): void {
    if (this.pending.get(request.taskId) === request) this.pending.delete(request.taskId)
  }

  isPending(taskId: string): boolean {
    return this.pending.has(taskId)
  }

  pendingTaskIds(): string[] {
    return [...this.pending.keys()]
  }

  /**
   * Attach an abort to the task's pending start, if there is one.
   * `accepted` must settle once the abort response arrives.
   * Returns the start request it was attached to.
   */
  noteAbort(taskId: string, accepted: Promise<boolean>): PendingStart | undefined {
    const request = this.pending.get(taskId)
    request?.aborts.push(accepted)
    return request
  }

  /** Whether any abort sent while this start was pending was accepted. */
  async wasAborted(request: PendingStart): Promise<boolean> {
    const results = await Promise.all(request.aborts)
    return results.some(Boolean)
  }
}
