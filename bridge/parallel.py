"""Ordered thread mapping with a bounded number of submitted tasks."""
from collections import deque
from concurrent.futures import ThreadPoolExecutor


def bounded_map(function, items, workers):
    if workers == 1:
        yield from map(function, items)
        return
    iterator = iter(items)
    pending = deque()
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="media-file") as pool:
        try:
            for _ in range(workers * 2):
                try:
                    item = next(iterator)
                except StopIteration:
                    break
                pending.append(pool.submit(function, item))
            while pending:
                yield pending.popleft().result()
                try:
                    item = next(iterator)
                except StopIteration:
                    continue
                pending.append(pool.submit(function, item))
        finally:
            for future in pending:
                future.cancel()
