from hub.core.storage.s3 import S3Provider
from hub.core.storage.local import LocalProvider
from hub.core.storage.memory import MemoryProvider
import pytest


enabled_storages = pytest.mark.parametrize(
    "storage",
    ["memory_storage", "local_storage", "s3_storage", "hub_cloud_storage"],
    indirect=True,
)


@pytest.fixture
def memory_storage(memory_path):
    return MemoryProvider(memory_path)


@pytest.fixture
def local_storage(local_path):
    return LocalProvider(local_path)


@pytest.fixture
def s3_storage(s3_path):
    return S3Provider(s3_path)


@pytest.fixture
def hub_cloud_storage(hub_cloud_path, hub_cloud_token):
    return S3Provider(hub_cloud_path, token=hub_cloud_token)


@pytest.fixture
def storage(request):
    """Used with parametrize to use all enabled storage fixtures."""
    return request.getfixturevalue(request.param)