
"""Database-backed orchestration logic for review jobs."""

from app.db.database import SessionLocal
from app.db.models import ReviewJobDB
from app.models.job import ReviewJob


class JobNotFoundError(Exception):
    """Raised when a requested review job does not exist."""


class OrchestratorService:
    def __init__(self, session_factory=None) -> None:
        # Allow tests to supply an isolated database session factory.
        self._session_factory = session_factory or SessionLocal

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


orchestrator_service = OrchestratorService()