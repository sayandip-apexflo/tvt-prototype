import unittest

from sqlalchemy.dialects import postgresql

from tvt_edge.camera.discovery import ValidationWorker


class _CaptureSession:
    def __init__(self) -> None:
        self.statement = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def scalar(self, statement):
        self.statement = statement
        return None


class _CaptureSessions:
    def __init__(self) -> None:
        self.session = _CaptureSession()

    def begin(self):
        return self.session


class CameraValidationClaimTests(unittest.TestCase):
    def test_postgresql_claim_locks_only_validation_attempt(self):
        sessions = _CaptureSessions()
        worker = ValidationWorker(sessions, keyring=None)

        self.assertIsNone(worker._claim())
        compiled = str(
            sessions.session.statement.compile(dialect=postgresql.dialect())
        )

        self.assertIn(
            "FOR UPDATE OF camera_validation_attempts SKIP LOCKED",
            compiled,
        )


if __name__ == "__main__":
    unittest.main()
