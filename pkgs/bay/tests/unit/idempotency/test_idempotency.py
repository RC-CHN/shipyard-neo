"""Unit tests for IdempotencyService.

Tests cover:
- Key validation
- Fingerprint computation
- Check/save flow
- Expiration handling
- Conflict detection
- Field persistence verification
"""

from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import IdempotencyConfig
from app.errors import ConflictError
from app.models.idempotency import IdempotencyKey
from app.services.idempotency import CachedResponse, IdempotencyService
from app.utils.datetime import utcnow


class TestKeyValidation:
    """Tests for idempotency key format validation."""

    def test_valid_alphanumeric_key(self):
        """Valid alphanumeric key passes."""
        assert IdempotencyService.validate_key("abc123") is True

    def test_valid_key_with_dash(self):
        """Key with dashes passes."""
        assert IdempotencyService.validate_key("abc-123-xyz") is True

    def test_valid_key_with_underscore(self):
        """Key with underscores passes."""
        assert IdempotencyService.validate_key("abc_123_xyz") is True

    def test_valid_uuid_format(self):
        """UUID-like key passes."""
        assert IdempotencyService.validate_key("550e8400-e29b-41d4-a716-446655440000") is True

    def test_empty_key_invalid(self):
        """Empty key fails."""
        assert IdempotencyService.validate_key("") is False

    def test_key_too_long(self):
        """Key over 128 chars fails."""
        long_key = "a" * 129
        assert IdempotencyService.validate_key(long_key) is False

    def test_key_max_length(self):
        """Key at exactly 128 chars passes."""
        max_key = "a" * 128
        assert IdempotencyService.validate_key(max_key) is True

    def test_key_with_spaces_invalid(self):
        """Key with spaces fails."""
        assert IdempotencyService.validate_key("abc 123") is False

    def test_key_with_special_chars_invalid(self):
        """Key with special chars fails."""
        assert IdempotencyService.validate_key("abc@123") is False
        assert IdempotencyService.validate_key("abc#123") is False
        assert IdempotencyService.validate_key("abc$123") is False


class TestFingerprintComputation:
    """Tests for request fingerprint computation."""

    def test_fingerprint_consistency(self):
        """Same inputs produce same fingerprint."""
        fp1 = IdempotencyService.compute_fingerprint(
            "/v1/sandboxes", "POST", '{"profile":"python"}'
        )
        fp2 = IdempotencyService.compute_fingerprint(
            "/v1/sandboxes", "POST", '{"profile":"python"}'
        )
        assert fp1 == fp2

    def test_fingerprint_different_body(self):
        """Different body produces different fingerprint."""
        fp1 = IdempotencyService.compute_fingerprint(
            "/v1/sandboxes", "POST", '{"profile":"python"}'
        )
        fp2 = IdempotencyService.compute_fingerprint("/v1/sandboxes", "POST", '{"profile":"data"}')
        assert fp1 != fp2

    def test_fingerprint_different_path(self):
        """Different path produces different fingerprint."""
        fp1 = IdempotencyService.compute_fingerprint(
            "/v1/sandboxes", "POST", '{"profile":"python"}'
        )
        fp2 = IdempotencyService.compute_fingerprint("/v1/other", "POST", '{"profile":"python"}')
        assert fp1 != fp2

    def test_fingerprint_different_method(self):
        """Different method produces different fingerprint."""
        fp1 = IdempotencyService.compute_fingerprint(
            "/v1/sandboxes", "POST", '{"profile":"python"}'
        )
        fp2 = IdempotencyService.compute_fingerprint("/v1/sandboxes", "PUT", '{"profile":"python"}')
        assert fp1 != fp2

    def test_fingerprint_is_sha256(self):
        """Fingerprint is 64 character hex string (SHA256)."""
        fp = IdempotencyService.compute_fingerprint("/v1/sandboxes", "POST", "{}")
        assert len(fp) == 64
        assert all(c in "0123456789abcdef" for c in fp)


class TestIdempotencyServiceCheck:
    """Tests for IdempotencyService.check() method."""

    @pytest.fixture
    def config(self):
        """Test idempotency config."""
        return IdempotencyConfig(enabled=True, ttl_hours=1)

    @pytest.fixture
    def service(self, db_session: AsyncSession, config: IdempotencyConfig):
        """Create service with test dependencies."""
        return IdempotencyService(db_session=db_session, config=config)

    @pytest.mark.asyncio
    async def test_check_returns_none_for_new_key(self, service: IdempotencyService):
        """New key returns None."""
        result = await service.check(
            owner="user1",
            key="new-key-123",
            path="/v1/sandboxes",
            method="POST",
            body="{}",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_check_returns_cached_response(
        self, service: IdempotencyService, db_session: AsyncSession
    ):
        """Existing key with matching fingerprint returns cached response."""
        # Insert a record directly
        fingerprint = IdempotencyService.compute_fingerprint(
            "/v1/sandboxes", "POST", '{"profile":"python"}'
        )
        record = IdempotencyKey(
            owner="user1",
            key="existing-key",
            request_fingerprint=fingerprint,
            response_snapshot='{"id": "sandbox-123"}',
            status_code=201,
            created_at=utcnow(),
            expires_at=utcnow() + timedelta(hours=1),
        )
        db_session.add(record)
        await db_session.flush()

        # Check should return cached response
        result = await service.check(
            owner="user1",
            key="existing-key",
            path="/v1/sandboxes",
            method="POST",
            body='{"profile":"python"}',
        )

        assert result is not None
        assert isinstance(result, CachedResponse)
        assert result.response == {"id": "sandbox-123"}
        assert result.status_code == 201

    @pytest.mark.asyncio
    async def test_check_raises_conflict_on_fingerprint_mismatch(
        self, service: IdempotencyService, db_session: AsyncSession
    ):
        """Different fingerprint raises ConflictError."""
        # Insert a record with one fingerprint
        fingerprint = IdempotencyService.compute_fingerprint(
            "/v1/sandboxes", "POST", '{"profile":"python"}'
        )
        record = IdempotencyKey(
            owner="user1",
            key="conflict-key",
            request_fingerprint=fingerprint,
            response_snapshot='{"id": "sandbox-123"}',
            status_code=201,
            created_at=utcnow(),
            expires_at=utcnow() + timedelta(hours=1),
        )
        db_session.add(record)
        await db_session.flush()

        # Check with different body should raise conflict
        with pytest.raises(ConflictError) as exc_info:
            await service.check(
                owner="user1",
                key="conflict-key",
                path="/v1/sandboxes",
                method="POST",
                body='{"profile":"data"}',  # Different profile
            )

        assert "Idempotency key already used" in str(exc_info.value.message)

    @pytest.mark.asyncio
    async def test_check_deletes_expired_key(
        self, service: IdempotencyService, db_session: AsyncSession
    ):
        """Expired key is deleted and returns None."""
        fingerprint = IdempotencyService.compute_fingerprint("/v1/sandboxes", "POST", "{}")
        record = IdempotencyKey(
            owner="user1",
            key="expired-key",
            request_fingerprint=fingerprint,
            response_snapshot='{"id": "sandbox-123"}',
            status_code=201,
            created_at=utcnow() - timedelta(hours=2),
            expires_at=utcnow() - timedelta(hours=1),  # Expired
        )
        db_session.add(record)
        await db_session.flush()

        # Check should return None (expired)
        result = await service.check(
            owner="user1",
            key="expired-key",
            path="/v1/sandboxes",
            method="POST",
            body="{}",
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_check_raises_conflict_for_invalid_key_format(self, service: IdempotencyService):
        """Invalid key format raises ConflictError."""
        with pytest.raises(ConflictError) as exc_info:
            await service.check(
                owner="user1",
                key="invalid key with spaces",
                path="/v1/sandboxes",
                method="POST",
                body="{}",
            )

        assert "Invalid Idempotency-Key format" in str(exc_info.value.message)

    @pytest.mark.asyncio
    async def test_check_owner_isolation(
        self, service: IdempotencyService, db_session: AsyncSession
    ):
        """Keys are isolated by owner."""
        fingerprint = IdempotencyService.compute_fingerprint("/v1/sandboxes", "POST", "{}")
        # Insert record for user1
        record = IdempotencyKey(
            owner="user1",
            key="shared-key",
            request_fingerprint=fingerprint,
            response_snapshot='{"id": "sandbox-user1"}',
            status_code=201,
            created_at=utcnow(),
            expires_at=utcnow() + timedelta(hours=1),
        )
        db_session.add(record)
        await db_session.flush()

        # user2 should get None for same key
        result = await service.check(
            owner="user2",  # Different owner
            key="shared-key",
            path="/v1/sandboxes",
            method="POST",
            body="{}",
        )

        assert result is None


class TestIdempotencyServiceSave:
    """Tests for IdempotencyService.save() method."""

    @pytest.fixture
    def config(self):
        """Test idempotency config."""
        return IdempotencyConfig(enabled=True, ttl_hours=1)

    @pytest.fixture
    def service(self, db_session: AsyncSession, config: IdempotencyConfig):
        """Create service with test dependencies."""
        return IdempotencyService(db_session=db_session, config=config)

    @pytest.mark.asyncio
    async def test_save_creates_record(self, service: IdempotencyService, db_session: AsyncSession):
        """Save creates a new idempotency record."""
        await service.save(
            owner="user1",
            key="new-save-key",
            path="/v1/sandboxes",
            method="POST",
            body='{"profile":"python"}',
            response={"id": "sandbox-new"},
            status_code=201,
        )

        # Verify record was created
        result = await service.check(
            owner="user1",
            key="new-save-key",
            path="/v1/sandboxes",
            method="POST",
            body='{"profile":"python"}',
        )

        assert result is not None
        assert result.response == {"id": "sandbox-new"}
        assert result.status_code == 201

    @pytest.mark.asyncio
    async def test_save_pydantic_model_response(self, service: IdempotencyService):
        """Save serializes Pydantic model responses."""
        from pydantic import BaseModel

        class TestResponse(BaseModel):
            id: str
            name: str

        response = TestResponse(id="test-123", name="test")

        await service.save(
            owner="user1",
            key="pydantic-key",
            path="/v1/test",
            method="POST",
            body="{}",
            response=response,
            status_code=200,
        )

        result = await service.check(
            owner="user1",
            key="pydantic-key",
            path="/v1/test",
            method="POST",
            body="{}",
        )

        assert result is not None
        assert result.response["id"] == "test-123"
        assert result.response["name"] == "test"

    @pytest.mark.asyncio
    async def test_save_persists_status_code(
        self, service: IdempotencyService, db_session: AsyncSession
    ):
        """Save should persist status_code correctly."""
        await service.save(
            owner="user1",
            key="status-code-key",
            path="/v1/test",
            method="POST",
            body="{}",
            response={"result": "ok"},
            status_code=202,  # Non-standard status code
        )

        result = await service.check(
            owner="user1",
            key="status-code-key",
            path="/v1/test",
            method="POST",
            body="{}",
        )

        assert result is not None
        assert result.status_code == 202


class TestIdempotencyServiceDisabled:
    """Tests for disabled idempotency service."""

    @pytest.fixture
    def disabled_config(self):
        """Disabled idempotency config."""
        return IdempotencyConfig(enabled=False, ttl_hours=1)

    @pytest.fixture
    def disabled_service(self, db_session: AsyncSession, disabled_config: IdempotencyConfig):
        """Create disabled service."""
        return IdempotencyService(db_session=db_session, config=disabled_config)

    @pytest.mark.asyncio
    async def test_check_returns_none_when_disabled(self, disabled_service: IdempotencyService):
        """Check returns None when disabled."""
        result = await disabled_service.check(
            owner="user1",
            key="any-key",
            path="/v1/sandboxes",
            method="POST",
            body="{}",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_save_is_noop_when_disabled(
        self, disabled_service: IdempotencyService, db_session: AsyncSession
    ):
        """Save does nothing when disabled."""
        await disabled_service.save(
            owner="user1",
            key="disabled-save-key",
            path="/v1/sandboxes",
            method="POST",
            body="{}",
            response={"id": "test"},
            status_code=201,
        )

        # Re-enable and check - should be None
        enabled_service = IdempotencyService(
            db_session=db_session,
            config=IdempotencyConfig(enabled=True, ttl_hours=1),
        )
        result = await enabled_service.check(
            owner="user1",
            key="disabled-save-key",
            path="/v1/sandboxes",
            method="POST",
            body="{}",
        )
        assert result is None
