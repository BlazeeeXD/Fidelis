"""Database-backed orchestration logic for review jobs."""

import threading
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.db.database import SessionLocal
from app.db.models import (
    RepositoryRuleDB, ReviewAgentRunDB, ReviewAuditDB, ReviewFindingDB, ReviewJobDB,
    ReviewJobOutboxDB, ReviewResultDB, ReviewAgentCheckpointDB,
)
from app.models.job import JobStatus, ReviewJob
from uuid import uuid4


class JobNotFoundError(Exception):
    """Raised when a requested review job does not exist."""


class InvalidJobTransitionError(Exception):
    """Raised when a worker requests a transition outside the job state machine."""


class OrchestratorService:
    def __init__(self, session_factory=None) -> None:
        # Allow tests to supply an isolated database session factory.
        self._session_factory = session_factory or SessionLocal
        # Serializes check-then-insert inside this process (see create_or_get_job).
        self._create_lock = threading.Lock()

    @staticmethod
    def _to_domain(db_job: ReviewJobDB) -> ReviewJob:
        """Convert a database model into the existing domain model."""
        return ReviewJob(
            id=db_job.id,
            repository=db_job.repository,
            pr_number=db_job.pr_number,
            commit_sha=db_job.commit_sha,
            status=db_job.status,
            created_at=db_job.created_at,
            started_at=db_job.started_at,
            completed_at=db_job.completed_at,
            error_message=db_job.error_message,
        )

    def create_job(
        self, repository: str, pr_number: int, commit_sha: str
    ) -> ReviewJob:
        """Create a review job and commit it to the database."""
        job = ReviewJob(
            repository=repository,
            pr_number=pr_number,
            commit_sha=commit_sha,
        )

        db = self._session_factory()
        try:
            db_job = ReviewJobDB(
                id=job.id,
                repository=job.repository,
                pr_number=job.pr_number,
                commit_sha=job.commit_sha,
                status=job.status,
                created_at=job.created_at,
                started_at=job.started_at,
                completed_at=job.completed_at,
                error_message=job.error_message,
            )
            db.add(db_job)
            db.commit()
            return self._to_domain(db_job)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def get_job(self, job_id: str) -> ReviewJob:
        """Retrieve a review job from the database."""
        db = self._session_factory()
        try:
            db_job = db.get(ReviewJobDB, job_id)
            if db_job is None:
                raise JobNotFoundError(job_id)
            return self._to_domain(db_job)
        finally:
            db.close()

    # ------------------------------------------------------------------
    # Week 2
    # ------------------------------------------------------------------

    @staticmethod
    def _find_active(db, repository: str, pr_number: int, commit_sha: str):
        """Newest non-FAILED job for this repo/PR/commit, or None."""
        stmt = (
            select(ReviewJobDB)
            .where(
                ReviewJobDB.repository == repository,
                ReviewJobDB.pr_number == pr_number,
                ReviewJobDB.commit_sha == commit_sha,
                ReviewJobDB.status != JobStatus.FAILED,
            )
            .order_by(ReviewJobDB.created_at.desc())
            .limit(1)
        )
        return db.execute(stmt).scalars().first()

    def create_or_get_job(
        self, repository: str, pr_number: int, commit_sha: str
    ) -> tuple[ReviewJob, bool]:
        """Idempotent create. Returns (job, created).

        A webhook redelivery for the same repo/PR/commit returns the existing
        job with created=False. A FAILED job does not block a retry.

        The process-local lock avoids ordinary check-then-insert races inside
        one worker process. The database partial unique index is the final
        cross-process guard; the IntegrityError branch reloads the winner.
        """
        with self._create_lock:
            db = self._session_factory()
            try:
                existing = self._find_active(db, repository, pr_number, commit_sha)
                if existing is not None:
                    # Jobs created before Week 3 may not have an outbox row yet.
                    # Add one for a still-QUEUED job so a replay can dispatch it.
                    if existing.status == JobStatus.QUEUED and db.get(ReviewJobOutboxDB, existing.id) is None:
                        db.add(ReviewJobOutboxDB(job_id=existing.id, created_at=datetime.now(UTC)))
                        db.commit()
                    return self._to_domain(existing), False

                job = ReviewJob(
                    repository=repository,
                    pr_number=pr_number,
                    commit_sha=commit_sha,
                )
                db_job = ReviewJobDB(
                    id=job.id,
                    repository=job.repository,
                    pr_number=job.pr_number,
                    commit_sha=job.commit_sha,
                    status=job.status,
                    created_at=job.created_at,
                    started_at=job.started_at,
                    completed_at=job.completed_at,
                    error_message=job.error_message,
                )
                db.add(db_job)
                # Job and dispatch intent are written in the same transaction.
                # The API publishes this outbox row to Redis after commit.
                db.add(ReviewJobOutboxDB(job_id=job.id, created_at=job.created_at))
                try:
                    db.commit()
                except IntegrityError:
                    db.rollback()
                    existing = self._find_active(
                        db, repository, pr_number, commit_sha
                    )
                    if existing is None:
                        raise
                    return self._to_domain(existing), False
                return self._to_domain(db_job), True
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

    def update_job_status(
        self,
        job_id: str,
        event: str,
        *,
        error_message: str | None = None,
        result_payload: dict | None = None,
        attempt: int | None = None,
        retry_after_seconds: float | None = None,
    ) -> ReviewJob:
        """Apply one worker lifecycle event and commit it to PostgreSQL.

        The reviewer worker never writes the database directly. It reports
        lifecycle events to this service, which owns the job record.
        """
        targets = {
            "started": JobStatus.RUNNING,
            "completed": JobStatus.COMPLETED,
            "retry_scheduled": JobStatus.QUEUED,
            "failed": JobStatus.FAILED,
        }
        if event not in targets:
            raise ValueError(f"unknown job status event: {event}")

        allowed = {
            JobStatus.QUEUED: {JobStatus.RUNNING},
            # RUNNING -> RUNNING is allowed for Celery late-ack redelivery
            # after a worker process dies during an attempt.
            JobStatus.RUNNING: {
                JobStatus.RUNNING, JobStatus.QUEUED,
                JobStatus.COMPLETED, JobStatus.FAILED,
            },
            JobStatus.COMPLETED: set(),
            JobStatus.FAILED: set(),
        }

        db = self._session_factory()
        try:
            db_job = db.execute(
                select(ReviewJobDB)
                .where(ReviewJobDB.id == job_id)
                .with_for_update()
            ).scalars().first()
            if db_job is None:
                raise JobNotFoundError(job_id)

            target = targets[event]
            if target not in allowed[db_job.status]:
                raise InvalidJobTransitionError(
                    f"job {job_id}: illegal transition {db_job.status.value} -> {target.value}"
                )

            now = datetime.now(UTC)
            previous_status = db_job.status
            db_job.status = target
            if event == "started":
                if db_job.started_at is None:
                    db_job.started_at = now
            elif event == "completed":
                db_job.completed_at = now
                db_job.error_message = None
                if result_payload is not None:
                    self._persist_result(db, db_job, result_payload)
            elif event == "retry_scheduled":
                # Preserve the first started_at across retries.
                db_job.error_message = error_message
            elif event == "failed":
                db_job.completed_at = now
                db_job.error_message = error_message

            db.add(ReviewAuditDB(
                job_id=job_id,
                event=event,
                from_status=previous_status.value,
                to_status=target.value,
                attempt=attempt,
                message=error_message,
                created_at=now,
                metadata_json=(
                    {"retry_after_seconds": retry_after_seconds}
                    if retry_after_seconds is not None else None
                ),
            ))
            db.commit()
            db.refresh(db_job)
            return self._to_domain(db_job)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()


    @staticmethod
    def _persist_result(db, db_job: ReviewJobDB, payload: dict) -> None:
        """Validate the untrusted worker result and persist it atomically."""
        if not isinstance(payload, dict):
            raise ValueError("review result must be a JSON object")
        expected = {
            "job_id": db_job.id,
            "repository": db_job.repository,
            "pr_number": db_job.pr_number,
            "commit_sha": db_job.commit_sha,
        }
        mismatches = [key for key, value in expected.items() if payload.get(key) != value]
        if mismatches:
            raise ValueError(f"review result identity mismatch: {', '.join(mismatches)}")

        reviewer = payload.get("reviewer")
        summary = payload.get("summary")
        findings = payload.get("findings")
        agent_runs = payload.get("agent_runs", [])
        if not isinstance(reviewer, dict) or not isinstance(summary, dict):
            raise ValueError("review result reviewer and summary must be objects")
        if not isinstance(findings, list) or not isinstance(agent_runs, list):
            raise ValueError("review result findings and agent_runs must be arrays")
        execution = payload.get("execution")
        if execution is not None:
            if not isinstance(execution, dict) or execution.get("strategy") not in {"sequential", "single_pass", "parallel", "mock"}:
                raise ValueError("review result execution metadata is invalid")
            for field in ("llm_requests", "duration_ms", "checkpoints_reused"):
                value = execution.get(field)
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ValueError(f"execution {field} must be a non-negative integer")
            for field in ("input_tokens", "output_tokens"):
                value = execution.get(field)
                if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                    raise ValueError(f"execution {field} must be null or a non-negative integer")
            selected = execution.get("selected_agents", [])
            if not isinstance(selected, list) or any(item not in {"STATIC_ANALYSIS", "SECURITY", "ARCHITECTURE", "CODE_STYLE", "GENERAL"} for item in selected):
                raise ValueError("execution selected_agents is invalid")
        total_findings = summary.get("total_findings")
        if isinstance(total_findings, bool) or not isinstance(total_findings, int) or total_findings != len(findings):
            raise ValueError("review result summary does not match findings")

        try:
            started_at = datetime.fromisoformat(str(payload["started_at"]).replace("Z", "+00:00"))
            completed_at = datetime.fromisoformat(str(payload["completed_at"]).replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("review result timestamps are invalid") from exc
        duration_ms = payload.get("duration_ms")
        if started_at.tzinfo is None or completed_at.tzinfo is None:
            raise ValueError("review result timestamps must include a timezone")
        if isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or duration_ms < 0:
            raise ValueError("review result duration_ms must be a non-negative integer")
        if completed_at < started_at:
            raise ValueError("review result completed_at precedes started_at")

        valid_agents = {"STATIC_ANALYSIS", "SECURITY", "ARCHITECTURE", "CODE_STYLE", "GENERAL"}
        valid_severities = {"HIGH", "MEDIUM", "LOW"}
        valid_categories = {"BUG", "SECURITY", "ARCHITECTURE", "MAINTAINABILITY", "STYLE"}
        seen_agents: set[str] = set()
        for run in agent_runs:
            if not isinstance(run, dict):
                raise ValueError("each agent run must be an object")
            agent = run.get("agent")
            if agent not in valid_agents or agent in seen_agents:
                raise ValueError("agent_runs contains an unknown or duplicate agent")
            seen_agents.add(agent)
            for field in ("duration_ms", "findings_count"):
                value = run.get(field)
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ValueError(f"agent run {field} must be a non-negative integer")
            for field in ("input_tokens", "output_tokens"):
                value = run.get(field)
                if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                    raise ValueError(f"agent run {field} must be null or a non-negative integer")
            model = run.get("model")
            if model is not None and (not isinstance(model, str) or len(model) > 128):
                raise ValueError("agent run model must be a string of at most 128 characters")
            for field in ("reused_checkpoint", "shared_call"):
                if field in run and not isinstance(run[field], bool):
                    raise ValueError(f"agent run {field} must be a boolean")

        for item in findings:
            if not isinstance(item, dict):
                raise ValueError("each finding must be an object")
            if item.get("agent", "GENERAL") not in valid_agents:
                raise ValueError("finding contains an unknown agent")
            if item.get("severity") not in valid_severities:
                raise ValueError("finding contains an unknown severity")
            if item.get("category") not in valid_categories:
                raise ValueError("finding contains an unknown category")
            message = item.get("message")
            if not isinstance(message, str) or not message.strip() or len(message) > 4000:
                raise ValueError("finding message must contain 1-4000 characters")
            file_name = item.get("file")
            if file_name is not None and (not isinstance(file_name, str) or len(file_name) > 1024):
                raise ValueError("finding file must be null or a path of at most 1024 characters")
            for field in ("line", "end_line"):
                value = item.get(field)
                if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
                    raise ValueError(f"finding {field} must be null or a positive integer")
            if item.get("line") is not None and file_name is None:
                raise ValueError("finding line requires a file")
            if item.get("end_line") is not None and (item.get("line") is None or item["end_line"] < item["line"]):
                raise ValueError("finding end_line must be >= line")
            confidence = item.get("confidence", 1.0)
            if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0.0 <= confidence <= 1.0:
                raise ValueError("finding confidence must be between 0 and 1")
            suggestion = item.get("suggestion")
            if suggestion is not None and (not isinstance(suggestion, str) or len(suggestion) > 4000):
                raise ValueError("finding suggestion must be null or at most 4000 characters")

        result = db.get(ReviewResultDB, db_job.id)
        values = dict(
            schema_version=str(payload.get("schema_version", "1.3")),
            reviewer_name=str(reviewer.get("name", "unknown"))[:128],
            reviewer_version=str(reviewer.get("version", "unknown"))[:32],
            reviewer_mode=str(reviewer.get("mode", "UNKNOWN"))[:16],
            started_at=started_at,
            completed_at=completed_at,
            duration_ms=duration_ms,
            payload=payload,
        )
        if result is None:
            db.add(ReviewResultDB(job_id=db_job.id, **values))
        else:
            for key, value in values.items():
                setattr(result, key, value)

        db.query(ReviewFindingDB).filter(ReviewFindingDB.job_id == db_job.id).delete()
        db.query(ReviewAgentRunDB).filter(ReviewAgentRunDB.job_id == db_job.id).delete()
        for run in agent_runs:
            db.add(ReviewAgentRunDB(
                job_id=db_job.id, agent=run["agent"], duration_ms=run["duration_ms"],
                findings_count=run["findings_count"], input_tokens=run.get("input_tokens"),
                output_tokens=run.get("output_tokens"), model=run.get("model"),
            ))
        for item in findings:
            db.add(ReviewFindingDB(
                id=str(item.get("id") or uuid4()),
                job_id=db_job.id,
                agent=str(item.get("agent", "GENERAL")),
                severity=str(item["severity"]),
                category=str(item["category"]),
                file=item.get("file"),
                line=item.get("line"),
                end_line=item.get("end_line"),
                message=item["message"].strip(),
                suggestion=item.get("suggestion"),
                confidence=float(item.get("confidence", 1.0)),
            ))

    def get_result(self, job_id: str) -> dict | None:
        db = self._session_factory()
        try:
            if db.get(ReviewJobDB, job_id) is None:
                raise JobNotFoundError(job_id)
            row = db.get(ReviewResultDB, job_id)
            return row.payload if row else None
        finally:
            db.close()


    def add_delivery_event(self, job_id: str, status: str, message: str | None = None, metadata: dict | None = None) -> None:
        """Append an external-delivery outcome without changing job status."""
        db = self._session_factory()
        try:
            if db.get(ReviewJobDB, job_id) is None:
                raise JobNotFoundError(job_id)
            db.add(ReviewAuditDB(
                job_id=job_id,
                event=f"github_review_{status}",
                from_status=None,
                to_status=None,
                attempt=None,
                message=(message or "")[:1000] or None,
                created_at=datetime.now(UTC),
                metadata_json=metadata or {},
            ))
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def list_audit_events(self, job_id: str) -> list[dict]:
        db = self._session_factory()
        try:
            if db.get(ReviewJobDB, job_id) is None:
                raise JobNotFoundError(job_id)
            rows = db.execute(
                select(ReviewAuditDB).where(ReviewAuditDB.job_id == job_id)
                .order_by(ReviewAuditDB.created_at.asc(), ReviewAuditDB.id.asc())
            ).scalars().all()
            return [{
                "id": row.id, "event": row.event, "from_status": row.from_status,
                "to_status": row.to_status, "attempt": row.attempt,
                "message": row.message, "created_at": row.created_at,
                "metadata": row.metadata_json,
            } for row in rows]
        finally:
            db.close()

    def summary_metrics(self, repository: str | None = None) -> dict:
        """Return aggregate dashboard data sourced from persisted jobs/results."""
        db = self._session_factory()
        try:
            job_query = db.query(ReviewJobDB)
            finding_query = db.query(ReviewFindingDB)
            if repository:
                job_query = job_query.filter(ReviewJobDB.repository == repository)
                finding_query = finding_query.join(ReviewJobDB, ReviewJobDB.id == ReviewFindingDB.job_id).filter(ReviewJobDB.repository == repository)
            jobs = job_query.all()
            findings = finding_query.all()
            completed = [j for j in jobs if j.status == JobStatus.COMPLETED]
            completed_ids = {j.id for j in completed}
            durations = [r.duration_ms for r in db.query(ReviewResultDB).filter(ReviewResultDB.job_id.in_(completed_ids)).all()] if completed_ids else []
            agent_runs = db.query(ReviewAgentRunDB).filter(ReviewAgentRunDB.job_id.in_(completed_ids)).all() if completed_ids else []
            result_rows = db.query(ReviewResultDB).filter(ReviewResultDB.job_id.in_(completed_ids)).all() if completed_ids else []
            legacy_tokens = {}
            for run in agent_runs:
                legacy_tokens[run.job_id] = (legacy_tokens.get(run.job_id, (0, 0))[0] + (run.input_tokens or 0), legacy_tokens.get(run.job_id, (0, 0))[1] + (run.output_tokens or 0))
            total_input_tokens = 0
            total_output_tokens = 0
            for result_row in result_rows:
                payload = result_row.payload if isinstance(result_row.payload, dict) else {}
                execution = payload.get("execution") if isinstance(payload.get("execution"), dict) else None
                if execution is not None:
                    total_input_tokens += execution.get("input_tokens") or 0
                    total_output_tokens += execution.get("output_tokens") or 0
                else:
                    legacy_input, legacy_output = legacy_tokens.get(result_row.job_id, (0, 0))
                    total_input_tokens += legacy_input
                    total_output_tokens += legacy_output
            # In single_pass every specialist result shares one model generation.
            # Those AgentRun rows deliberately have duration_ms=0; exclude them
            # from per-agent runtime statistics rather than reporting false zeroes.
            shared_agent_keys: set[tuple[str, str]] = set()
            for result_row in result_rows:
                payload = result_row.payload if isinstance(result_row.payload, dict) else {}
                for run_payload in payload.get("agent_runs", []) if isinstance(payload.get("agent_runs"), list) else []:
                    if isinstance(run_payload, dict) and run_payload.get("shared_call") is True:
                        shared_agent_keys.add((result_row.job_id, str(run_payload.get("agent", ""))))
            by_agent_duration = {}
            for agent in ("STATIC_ANALYSIS", "SECURITY", "ARCHITECTURE", "CODE_STYLE", "GENERAL"):
                values = [
                    run.duration_ms for run in agent_runs
                    if run.agent == agent and (run.job_id, run.agent) not in shared_agent_keys
                ]
                if values:
                    by_agent_duration[agent] = round(sum(values) / len(values), 2)
            slowest_agent = max(by_agent_duration, key=by_agent_duration.get) if by_agent_duration else None
            return {
                "total_jobs": len(jobs),
                "queued_jobs": sum(j.status == JobStatus.QUEUED for j in jobs),
                "running_jobs": sum(j.status == JobStatus.RUNNING for j in jobs),
                "completed_jobs": len(completed),
                "failed_jobs": sum(j.status == JobStatus.FAILED for j in jobs),
                "total_findings": len(findings),
                "by_severity": {key: sum(f.severity == key for f in findings) for key in ("HIGH", "MEDIUM", "LOW")},
                "by_category": {key: sum(f.category == key for f in findings) for key in ("BUG", "SECURITY", "ARCHITECTURE", "MAINTAINABILITY", "STYLE")},
                "by_agent": {key: sum(f.agent == key for f in findings) for key in ("STATIC_ANALYSIS", "SECURITY", "ARCHITECTURE", "CODE_STYLE", "GENERAL")},
                "average_review_duration_ms": round(sum(durations) / len(durations), 2) if durations else None,
                "average_agent_duration_ms": by_agent_duration,
                "slowest_agent": slowest_agent,
                "total_input_tokens": total_input_tokens,
                "total_output_tokens": total_output_tokens,
            }
        finally:
            db.close()

    def get_repository_rules(self, repository: str) -> list[dict]:
        db = self._session_factory()
        try:
            rows = db.execute(
                select(RepositoryRuleDB).where(RepositoryRuleDB.repository == repository)
                .order_by(RepositoryRuleDB.confidence.desc(), RepositoryRuleDB.rule_key.asc())
            ).scalars().all()
            return [{
                "id": row.id, "repository": row.repository, "rule_key": row.rule_key,
                "description": row.description, "confidence": row.confidence,
                "evidence_count": row.evidence_count, "source_prs": row.source_prs,
                "updated_at": row.updated_at,
            } for row in rows]
        finally:
            db.close()

    def upsert_repository_rules(self, repository: str, rules: list[dict]) -> list[dict]:
        db = self._session_factory()
        try:
            now = datetime.now(UTC)
            for item in rules:
                key = str(item["rule_key"]).strip()[:128]
                description = str(item["description"]).strip()
                if not key or not description:
                    continue
                stmt = select(RepositoryRuleDB).where(
                    RepositoryRuleDB.repository == repository,
                    RepositoryRuleDB.rule_key == key,
                )
                row = db.execute(stmt).scalars().first()
                if row is None:
                    row = RepositoryRuleDB(
                        id=str(uuid4()), repository=repository, rule_key=key,
                        description=description, confidence=float(item.get("confidence", 0.5)),
                        evidence_count=max(1, int(item.get("evidence_count", 1))),
                        source_prs=item.get("source_prs") or [], updated_at=now,
                    )
                    db.add(row)
                else:
                    row.description = description
                    row.confidence = max(0.0, min(1.0, float(item.get("confidence", row.confidence))))
                    row.evidence_count = max(row.evidence_count, int(item.get("evidence_count", row.evidence_count)))
                    row.source_prs = item.get("source_prs") or row.source_prs
                    row.updated_at = now
            db.commit()
            return self.get_repository_rules(repository)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def create_replay_job(self, job_id: str) -> tuple[ReviewJob, bool]:
        """Create a fresh job for a replay, preserving the original history."""
        with self._create_lock:
            db = self._session_factory()
            try:
                original = db.get(ReviewJobDB, job_id)
                if original is None:
                    raise JobNotFoundError(job_id)
                active = db.execute(
                    select(ReviewJobDB).where(
                        ReviewJobDB.repository == original.repository,
                        ReviewJobDB.pr_number == original.pr_number,
                        ReviewJobDB.commit_sha == original.commit_sha,
                        ReviewJobDB.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
                    ).order_by(ReviewJobDB.created_at.desc()).limit(1)
                ).scalars().first()
                if active is not None:
                    return self._to_domain(active), False
                job = ReviewJob(repository=original.repository, pr_number=original.pr_number, commit_sha=original.commit_sha)
                row = ReviewJobDB(
                    id=job.id, repository=job.repository, pr_number=job.pr_number,
                    commit_sha=job.commit_sha, status=job.status, created_at=job.created_at,
                    started_at=None, completed_at=None, error_message=None,
                )
                db.add(row)
                db.add(ReviewJobOutboxDB(job_id=job.id, created_at=job.created_at))
                db.add(ReviewAuditDB(
                    job_id=job.id, event="replayed", from_status=None, to_status="QUEUED",
                    attempt=None, message=f"Replay of {job_id}", created_at=job.created_at,
                    metadata_json={"original_job_id": job_id},
                ))
                db.commit()
                return self._to_domain(row), True
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

    def list_jobs(
        self,
        repository: str | None = None,
        pr_number: int | None = None,
        status: JobStatus | None = None,
        limit: int = 50,
    ) -> list[ReviewJob]:
        """Newest first. For debugging and the later dashboard."""
        db = self._session_factory()
        try:
            stmt = select(ReviewJobDB)
            if repository is not None:
                stmt = stmt.where(ReviewJobDB.repository == repository)
            if pr_number is not None:
                stmt = stmt.where(ReviewJobDB.pr_number == pr_number)
            if status is not None:
                stmt = stmt.where(ReviewJobDB.status == status)
            stmt = stmt.order_by(ReviewJobDB.created_at.desc()).limit(limit)
            return [self._to_domain(j) for j in db.execute(stmt).scalars()]
        finally:
            db.close()


    def list_agent_checkpoints(self, job_id: str, context_fingerprint: str) -> list[dict]:
        """Return checkpoints only when their exact review-context fingerprint matches."""
        db = self._session_factory()
        try:
            if db.get(ReviewJobDB, job_id) is None:
                raise JobNotFoundError(job_id)
            rows = (db.query(ReviewAgentCheckpointDB)
                    .filter(ReviewAgentCheckpointDB.job_id == job_id,
                            ReviewAgentCheckpointDB.context_fingerprint == context_fingerprint)
                    .all())
            return [{"agent": row.agent, "payload": row.payload, "updated_at": row.updated_at.isoformat()} for row in rows]
        finally:
            db.close()

    def save_agent_checkpoint(self, job_id: str, agent: str, context_fingerprint: str, payload: dict) -> None:
        """Upsert one completed specialist result in the durable job database."""
        db = self._session_factory()
        try:
            if db.get(ReviewJobDB, job_id) is None:
                raise JobNotFoundError(job_id)
            row = db.get(ReviewAgentCheckpointDB, (job_id, agent))
            if row is None:
                row = ReviewAgentCheckpointDB(
                    job_id=job_id, agent=agent, context_fingerprint=context_fingerprint,
                    payload=payload, updated_at=datetime.now(UTC),
                )
                db.add(row)
            else:
                row.context_fingerprint = context_fingerprint
                row.payload = payload
                row.updated_at = datetime.now(UTC)
            db.add(ReviewAuditDB(
                job_id=job_id, event="agent_checkpoint_saved", from_status=None, to_status=None,
                attempt=None, message=f"Completed {agent} checkpoint", created_at=datetime.now(UTC),
                metadata_json={"agent": agent},
            ))
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()


orchestrator_service = OrchestratorService()
