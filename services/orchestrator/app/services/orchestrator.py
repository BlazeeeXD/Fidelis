"""Database-backed orchestration logic for review jobs."""

import threading

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db.database import SessionLocal
from app.db.models import ReviewJobDB
from app.models.job import JobStatus, ReviewJob


class JobNotFoundError(Exception):
    """Raised when a requested review job does not exist."""


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

        The lock covers concurrent requests inside this process. If P4 later
        adds the unique partial index from the contract doc, the IntegrityError
        branch covers multiple processes as well.
        """
        with self._create_lock:
            db = self._session_factory()
            try:
                existing = self._find_active(db, repository, pr_number, commit_sha)
                if existing is not None:
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


orchestrator_service = OrchestratorService()