"""REGRESSION: the chunk plan is floor-based, and the last chunk absorbs the rest.

Ceil-based math looks obviously correct and makes the API answer "invalid chunk
count" for every file over the per-chunk ceiling. The invariant asserted here is
the one the platform enforces:

    total_chunk_count == video_size // chunk_size

plus: the ranges must cover the file exactly, with the remainder living in the
LAST chunk rather than in an extra one.
"""

import pytest

from postvox.errors import PublishError
from postvox.platforms.tiktok.upload import plan_chunks

MiB = 1024 * 1024
CAP = 20 * MiB          # the empirical reliability setting, not the 64 MiB doc ceiling


@pytest.mark.parametrize("size", [
    1,
    5 * MiB,
    20 * MiB,               # exactly the cap: still one chunk
    20 * MiB + 1,           # one byte over: the floor math starts here
    25 * MiB,
    41 * MiB,
    60 * MiB,
    110 * MiB,              # the size that produced a 403 before transcoding
    999 * MiB + 7,
])
def test_floor_relationship_holds(size):
    plan = plan_chunks(size, CAP)
    assert plan.video_size == size
    # THE invariant. If this fails the API returns "invalid chunk count".
    assert plan.total_chunk_count == plan.video_size // plan.chunk_size


@pytest.mark.parametrize("size", [20 * MiB + 1, 25 * MiB, 41 * MiB, 110 * MiB])
def test_last_chunk_absorbs_the_remainder(size):
    plan = plan_chunks(size, CAP)
    ranges = plan.ranges()
    assert len(ranges) == plan.total_chunk_count
    assert sum(length for _, length in ranges) == size, "ranges must cover the file"
    heads = [length for _, length in ranges[:-1]]
    assert all(length == plan.chunk_size for length in heads)
    assert ranges[-1][1] >= plan.chunk_size, "the remainder goes in the LAST chunk"
    # And it is never split off into an extra short chunk of its own.
    assert ranges[-1][1] < 2 * plan.chunk_size


def test_ranges_are_contiguous_from_zero():
    plan = plan_chunks(41 * MiB, CAP)
    offset = 0
    for start, length in plan.ranges():
        assert start == offset
        offset += length
    assert offset == plan.video_size


def test_small_file_is_a_single_chunk():
    plan = plan_chunks(3 * MiB, CAP)
    assert plan.total_chunk_count == 1
    assert plan.chunk_size == 3 * MiB
    assert plan.as_source_info() == {
        "source": "FILE_UPLOAD",
        "video_size": 3 * MiB,
        "chunk_size": 3 * MiB,
        "total_chunk_count": 1,
    }


def test_ceil_math_would_have_been_wrong():
    """Documents the bug this function exists to avoid.

    For 41 MiB at a 20 MiB cap, ceil gives 3 pieces of 20 MiB — and
    ``41 // 20 == 2``, so ``total_chunk_count`` would not equal
    ``video_size // chunk_size`` and the init call would be rejected.
    """
    size, cap = 41 * MiB, 20 * MiB
    ceil_count = -(-size // cap)
    assert ceil_count != size // cap, "the naive plan really is inconsistent"
    plan = plan_chunks(size, cap)
    assert plan.total_chunk_count == size // plan.chunk_size


def test_zero_byte_video_is_refused():
    with pytest.raises(PublishError):
        plan_chunks(0, CAP)
