import pytest

from drella.storage import LocalStorage, NotFound


@pytest.fixture
def store(tmp_path):
    return LocalStorage(tmp_path / "s")


def test_put_get_streams(store):
    assert store.put("jobs/1/log.txt", iter([b"hello ", b"world"])) == 11
    assert b"".join(store.get("jobs/1/log.txt")) == b"hello world"
    assert store.exists("jobs/1/log.txt")


def test_large_object_is_chunked(store):
    data = b"x" * (200 * 1024)
    store.put("big", [data])
    chunks = list(store.get("big"))
    assert len(chunks) > 1 and b"".join(chunks) == data


def test_put_overwrites(store):
    store.put("k", [b"one"])
    store.put("k", [b"two"])
    assert b"".join(store.get("k")) == b"two"


def test_get_missing_raises_immediately(store):
    with pytest.raises(NotFound):
        store.get("nope")


def test_list_by_prefix(store):
    for k in ["jobs/1/outputs/b", "jobs/1/outputs/a", "jobs/2/outputs/a", "jobs/1/log.txt"]:
        store.put(k, [b"."])
    assert store.list("jobs/1/outputs/") == ["jobs/1/outputs/a", "jobs/1/outputs/b"]
    assert len(store.list()) == 4


def test_delete(store):
    store.put("k", [b"."])
    store.delete("k")
    store.delete("k")  # missing is fine
    assert not store.exists("k")


def test_failed_put_leaves_nothing(store):
    def boom():
        yield b"partial"
        raise RuntimeError

    with pytest.raises(RuntimeError):
        store.put("k", boom())
    assert store.list() == []


@pytest.mark.parametrize("key", ["", "/abs", "../escape", "a/../../b", "dir/"])
def test_rejects_bad_keys(store, key):
    with pytest.raises(ValueError):
        store.put(key, [b"."])
