"""Thin wrapper over pinned cron.jobs; the ledger owns task links and missed slots."""

from datetime import datetime, timezone
import json
from uuid import uuid4

from hermes_dots.ledger import MissedRun


class HermesScheduler:
    def __init__(self, ledger, api=None):
        if api is None:
            from cron import jobs as api
        self.ledger, self.api = ledger, api

    def pause(self, job_id): return self.api.pause_job(job_id)
    def resume(self, job_id): return self.api.resume_job(job_id)
    def remove(self, job_id): return self.api.remove_job(job_id)
    def list(self): return self.api.list_jobs(include_disabled=True)

    def create(self, engine, task_id, instruction, time, timezone, end_date=None):
        engine._require_confirmed(task_id)
        if timezone != engine.settings.get("timezone"):
            raise ValueError("The pinned scheduler uses the configured profile time zone")
        job = self.api.create_job(prompt=instruction, schedule=time, workdir="/workspace",
                                  enabled_toolsets=["hermes_dots", "file", "terminal", "web", "delegation"],
                                  deliver="local", no_agent=False)
        try:
            engine.register_resource(task_id, "schedule", job["id"], instruction=instruction,
                                     time=time, timezone=timezone, end_date=end_date,
                                     next_run_at=job["next_run_at"], requires_model=True)
        except Exception:
            self.remove(job["id"])
            raise
        return {**job, "task_id": task_id, "timezone": timezone, "end_date": end_date}

    def ensure_tick(self, script, workdir):
        existing = next((j for j in self.list() if j.get("name") == "hermes-dots-tick"), None)
        if existing:
            if existing.get("no_agent") is not True or existing.get("script") != script:
                raise ValueError("Existing dots tick does not match the installed script")
            return existing
        return self.api.create_job(prompt=None, schedule="1m", name="hermes-dots-tick",
                                   script=script, workdir=str(workdir), no_agent=True, deliver="local")

    def capture_missed(self, *, now=None, limited=False):
        """Copy known skipped slots. Unknown/running executions are never inferred as misses."""
        now = now or datetime.now(timezone.utc)
        jobs = {job["id"]: job for job in self.list()}
        with self.ledger.transaction():
            rows = self.ledger.connection.execute("SELECT * FROM task_resources WHERE kind='schedule'").fetchall()
            for row in rows:
                job = jobs.get(row["id"])
                if job is None: continue
                data = json.loads(row["data"])
                end = data.get("end_date")
                if end and datetime.fromisoformat(end) <= now:
                    self.pause(job["id"])
                    self.ledger.connection.execute("UPDATE task_resources SET state='expired' WHERE kind='schedule' AND id=?", (job["id"],))
                    continue
                slot = data.get("next_run_at")
                current = job.get("next_run_at")
                # A paused job keeps its original next_run_at. We advance only our receipt cursor.
                boundary = now.isoformat() if limited or row["state"] in {"limit_paused", "paused"} else current
                while slot and boundary and datetime.fromisoformat(slot) < datetime.fromisoformat(boundary):
                    execution = job.get("latest_execution") or {}
                    exact = execution.get("scheduled_instant") == slot
                    if exact and execution.get("status") not in {"completed", "failed", "cancelled"}:
                        break
                    if not exact or execution.get("status") != "completed":
                        self.ledger.record_missed_run(MissedRun(uuid4().hex, row["task_id"], job["id"], slot,
                                                              "model_limit" if limited else "paused" if row["state"] == "paused" else "scheduler_missed"))
                    following = self.api.compute_next_run(job["schedule"], slot)
                    if following is None or datetime.fromisoformat(following) <= datetime.fromisoformat(slot):
                        slot = None
                        break
                    slot = following
                data["next_run_at"] = slot or current
                self.ledger.connection.execute("UPDATE task_resources SET data=? WHERE kind='schedule' AND id=?",
                                               (json.dumps(data), job["id"]))
