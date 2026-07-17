"""Sync S4: S3 adapter.

Two tiers:
- TestS3Conformance: the shared suite against a REAL MinIO from
  deploy/docker/docker-compose.sync-test.yml — AUTO-SKIPS when MinIO is not
  reachable (CI and dockerless machines stay green).
- TestS3FailureInjection: botocore Stubber — pagination, 5xx retry,
  404 classification, lock contention. ALWAYS runs, no docker, no network.
"""
import io
import os
import uuid
from datetime import UTC, datetime

import httpx
import pytest

from tests.sync_conformance import AdapterConformance
from tiro.sync.adapters import base as adapter_base
from tiro.sync.adapters.base import LOCK_KEY, AdapterError, KeyMissing, make_lock_payload
from tiro.sync.adapters.s3 import S3Adapter

MINIO_URL = os.environ.get("TIRO_TEST_MINIO_URL", "http://localhost:9000")
MINIO_ACCESS = os.environ.get("TIRO_TEST_MINIO_ACCESS_KEY", "tiro-sync-test")
MINIO_SECRET = os.environ.get("TIRO_TEST_MINIO_SECRET_KEY", "tiro-sync-test-secret")
BUCKET = "tiro-sync-conformance"


def _minio_available() -> bool:
    try:
        return httpx.get(f"{MINIO_URL}/minio/health/live", timeout=1.0).status_code == 200
    except Exception:
        return False


requires_minio = pytest.mark.skipif(
    not _minio_available(),
    reason=(
        "MinIO not reachable — start it with: "
        "docker compose -f deploy/docker/docker-compose.sync-test.yml up -d minio"
    ),
)


@pytest.fixture(scope="session")
def minio_bucket():
    import boto3
    from botocore.exceptions import ClientError

    client = boto3.client(
        "s3", endpoint_url=MINIO_URL, aws_access_key_id=MINIO_ACCESS,
        aws_secret_access_key=MINIO_SECRET, region_name="us-east-1",
    )
    try:
        client.create_bucket(Bucket=BUCKET)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
            raise
    return BUCKET


@requires_minio
class TestS3Conformance(AdapterConformance):
    @pytest.fixture
    def make_adapter(self, minio_bucket):
        run_prefix = f"t-{uuid.uuid4().hex}/"  # per-test isolation in the shared bucket

        def make(device_id: str) -> S3Adapter:
            return S3Adapter(
                endpoint_url=MINIO_URL, bucket=minio_bucket,
                access_key=MINIO_ACCESS, secret_key=MINIO_SECRET,
                device_id=device_id, prefix=run_prefix,
            )

        return make


def _stubbed_adapter(page_size: int = 1000) -> tuple:
    """Adapter over a never-connecting client + its Stubber."""
    import boto3
    from botocore.stub import Stubber

    client = boto3.client(
        "s3", endpoint_url="http://stub.invalid", region_name="us-east-1",
        aws_access_key_id="stub", aws_secret_access_key="stub",
    )
    adapter = S3Adapter(
        endpoint_url="http://stub.invalid", bucket="bkt", access_key="stub",
        secret_key="stub", device_id="dev-a", client=client, page_size=page_size,
    )
    return adapter, Stubber(client)


def _body(data: bytes):
    from botocore.response import StreamingBody

    return StreamingBody(io.BytesIO(data), len(data))


@pytest.fixture
def no_sleep(monkeypatch):
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(adapter_base, "_SLEEP", fake_sleep)
    return slept


class TestS3FailureInjection:
    async def test_list_pagination_follows_continuation_token(self):
        adapter, stub = _stubbed_adapter(page_size=1)
        stub.add_response(
            "list_objects_v2",
            {"IsTruncated": True,
             "Contents": [{"Key": "journal/dev-a/000000000001.age"}],
             "NextContinuationToken": "tok-1"},
            {"Bucket": "bkt", "Prefix": "journal/", "MaxKeys": 1},
        )
        stub.add_response(
            "list_objects_v2",
            {"IsTruncated": False,
             "Contents": [{"Key": "journal/dev-b/000000000001.age"}]},
            {"Bucket": "bkt", "Prefix": "journal/", "MaxKeys": 1,
             "ContinuationToken": "tok-1"},
        )
        with stub:
            keys = await adapter.list("journal/")
        assert keys == [
            "journal/dev-a/000000000001.age",
            "journal/dev-b/000000000001.age",
        ]

    async def test_get_5xx_retried_then_succeeds(self, no_sleep):
        adapter, stub = _stubbed_adapter()
        for _ in range(2):
            stub.add_client_error(
                "get_object", service_error_code="InternalError",
                http_status_code=500,
                expected_params={"Bucket": "bkt", "Key": "format.json"},
            )
        stub.add_response(
            "get_object", {"Body": _body(b"v1")},
            {"Bucket": "bkt", "Key": "format.json"},
        )
        with stub:
            assert await adapter.get("format.json") == b"v1"
        assert len(no_sleep) == 2  # two backoff sleeps

    async def test_5xx_exhausts_then_raises_adapter_error(self, no_sleep):
        adapter, stub = _stubbed_adapter()
        for _ in range(3):
            stub.add_client_error(
                "put_object", service_error_code="ServiceUnavailable",
                http_status_code=503,
            )
        with stub, pytest.raises(AdapterError):
            await adapter.put("format.json", b"x")
        assert len(no_sleep) == 2

    async def test_get_404_raises_keymissing_without_retry(self, no_sleep):
        adapter, stub = _stubbed_adapter()
        stub.add_client_error(
            "get_object", service_error_code="NoSuchKey", http_status_code=404,
            expected_params={"Bucket": "bkt", "Key": "objects/aa/x.age"},
        )
        with stub, pytest.raises(KeyMissing):
            await adapter.get("objects/aa/x.age")
        assert no_sleep == []  # 4xx never retries (decision #2)
        stub.assert_no_pending_responses()

    async def test_403_not_retried_raises_adapter_error(self, no_sleep):
        adapter, stub = _stubbed_adapter()
        stub.add_client_error(
            "put_object", service_error_code="AccessDenied", http_status_code=403,
        )
        with stub, pytest.raises(AdapterError):
            await adapter.put("format.json", b"x")
        assert no_sleep == []
        stub.assert_no_pending_responses()

    async def test_lock_precondition_failed_held_fresh_returns_false(self):
        adapter, stub = _stubbed_adapter()
        stub.add_client_error(
            "put_object", service_error_code="PreconditionFailed",
            http_status_code=412,
        )
        fresh = make_lock_payload("dev-other", 300)
        stub.add_response(
            "get_object", {"Body": _body(fresh)},
            {"Bucket": "bkt", "Key": LOCK_KEY},
        )
        with stub:
            assert await adapter.lock(ttl_s=300) is False

    async def test_lock_steals_expired_then_acquires(self):
        adapter, stub = _stubbed_adapter()
        stale = make_lock_payload("dev-dead", 60, now=datetime(2020, 1, 1, tzinfo=UTC))
        stub.add_client_error(
            "put_object", service_error_code="PreconditionFailed",
            http_status_code=412,
        )
        stub.add_response(
            "get_object", {"Body": _body(stale)}, {"Bucket": "bkt", "Key": LOCK_KEY}
        )
        stub.add_response("delete_object", {}, {"Bucket": "bkt", "Key": LOCK_KEY})
        stub.add_response("put_object", {}, None)  # conditional retry succeeds
        with stub:
            assert await adapter.lock(ttl_s=300) is True

    def test_encrypt_default_on(self):
        assert S3Adapter.encrypt_default is True  # spec §5
        assert S3Adapter.name == "s3"
