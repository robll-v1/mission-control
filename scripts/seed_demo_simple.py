#!/usr/bin/env python3
"""Generate demo review data without importing app modules."""
import json
import sqlite3
import sys
import time
from pathlib import Path

def create_demo_data(db_path: str):
    """Create demo task with review findings directly via SQL."""
    conn = sqlite3.connect(db_path)

    # Clear existing demo data
    conn.execute("DELETE FROM tasks WHERE id LIKE 'demo%'")
    conn.execute("DELETE FROM runs WHERE task_id LIKE 'demo%'")
    conn.commit()

    task_id = 'demo001'
    now = time.time()

    # Create review findings
    findings = [
        {
            'severity': 'high',
            'summary': 'Webhook signature verification missing for critical payment events',
            'path': 'src/webhooks/stripe_handler.py',
            'line': 45,
            'detail': 'The webhook handler processes payment.succeeded events without verifying the Stripe signature. An attacker could send forged webhook requests to trigger unauthorized payment confirmations or account credits.'
        },
        {
            'severity': 'high',
            'summary': 'SQL injection vulnerability in transaction lookup',
            'path': 'src/models/transaction.py',
            'line': 128,
            'detail': 'The find_by_external_id method builds a SQL query using string formatting with unsanitized user input from the webhook payload. Use parameterized queries instead.'
        },
        {
            'severity': 'medium',
            'summary': 'Insufficient error handling may expose internal state',
            'path': 'src/webhooks/stripe_handler.py',
            'line': 78,
            'detail': 'Database connection errors are caught but the full exception message (including connection string components) is returned in the HTTP response. Log the error internally and return a generic 500 message to the client.'
        },
        {
            'severity': 'medium',
            'summary': 'Race condition in idempotency check',
            'path': 'src/webhooks/stripe_handler.py',
            'line': 62,
            'detail': 'The idempotency key check and insert happen in separate transactions. Two concurrent requests with the same event ID could both pass the check and process the webhook twice. Use a unique constraint or SELECT FOR UPDATE.'
        },
        {
            'severity': 'low',
            'summary': 'Hardcoded retry limit should be configurable',
            'path': 'src/webhooks/stripe_handler.py',
            'line': 23,
            'detail': 'MAX_RETRIES = 3 is defined as a constant. Consider making this configurable via environment variable or application config for operational flexibility.'
        },
        {
            'severity': 'low',
            'summary': 'Missing type hint for webhook_data parameter',
            'path': 'src/webhooks/stripe_handler.py',
            'line': 38,
            'detail': 'The process_webhook function signature lacks a type annotation for webhook_data. Adding dict[str, Any] improves code maintainability and enables better IDE support.'
        }
    ]

    review_result = {
        'verdict': 'concerns',
        'summary': 'Found 2 high-severity security issues and 4 additional concerns',
        'findings': findings,
        'metrics': {'total_findings': 6, 'high': 2, 'medium': 2, 'low': 2}
    }

    task_data = {
        'id': task_id,
        'repo_path': '/Users/demo/projects/payment-service',
        'source_type': 'pull_request',
        'pr_url': 'https://github.com/example/payment-service/pull/142',
        'base': 'main',
        'title': 'Add Stripe webhook handler for payment events',
        'review_focus': 'Security, error handling, and data validation',
        'backend': 'opencode',
        'tags': ['security', 'payments'],
        'status': 'completed',
        'current_stage': 'completed',
        'created_at': now - 3600,
        'updated_at': now - 120,
        'last_run_id': 'run001',
        'latest_review_result': review_result
    }

    run_data = {
        'id': 'run001',
        'task_id': task_id,
        'backend': 'opencode',
        'round_index': 1,
        'review_note': 'Initial security review',
        'review_revision': 'a3f9c8b',
        'backend_session_id': 'sess_xyz789',
        'status': 'completed',
        'review_result': review_result,
        'metrics': {
            'duration_seconds': 127,
            'tokens_used': 8450,
            'model': 'gpt-6-astra'
        },
        'started_at': now - 300,
        'ended_at': now - 173
    }

    # Insert task and run
    conn.execute('INSERT INTO tasks (id, data) VALUES (?, ?)',
                 (task_id, json.dumps(task_data)))
    conn.execute('INSERT INTO runs (id, task_id, data) VALUES (?, ?, ?)',
                 ('run001', task_id, json.dumps(run_data)))
    conn.commit()
    conn.close()

    print(f'Created demo task {task_id}')
    print(f'Database: {Path(db_path).absolute()}')
    print(f'View at: http://localhost:5199/review/{task_id}')

if __name__ == '__main__':
    db_path = sys.argv[1] if len(sys.argv) > 1 else 'data/amc.db'
    create_demo_data(db_path)
