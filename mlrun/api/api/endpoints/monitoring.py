import fastapi
import datetime
import linecache


import mlrun
import mlrun.api.utils.periodic
import tracemalloc

router = fastapi.APIRouter()


@router.post("/monitoring/memory/enable")
def enable_memory_monitoring(
    interval: int = mlrun.mlconf.httpdb.monitoring.memory.interval,
    number_of_frames: int = mlrun.mlconf.httpdb.monitoring.memory.number_of_frames,
):
    tracemalloc.start(number_of_frames)
    mlrun.api.utils.periodic.run_function_periodically(interval, _run_memory_monitoring)


def _run_memory_monitoring():
    now = datetime.datetime.utcnow().isoformat()
    filename = f"memory-snapshot-{now}"
    snapshot = tracemalloc.take_snapshot()
    snapshot.dump(filename)
    display_top(snapshot)


def display_top(snapshot, key_type="lineno", limit=10):
    snapshot = snapshot.filter_traces(
        (
            tracemalloc.Filter(False, "<frozen importlib._bootstrap>"),
            tracemalloc.Filter(False, "<unknown>"),
        )
    )
    top_stats = snapshot.statistics(key_type)

    print("Top %s lines" % limit)
    for index, stat in enumerate(top_stats[:limit], 1):
        frame = stat.traceback[0]
        print(
            "#%s: %s:%s: %.1f KiB"
            % (index, frame.filename, frame.lineno, stat.size / 1024)
        )
        line = linecache.getline(frame.filename, frame.lineno).strip()
        if line:
            print("    %s" % line)

    other = top_stats[limit:]
    if other:
        size = sum(stat.size for stat in other)
        print("%s other: %.1f KiB" % (len(other), size / 1024))
    total = sum(stat.size for stat in top_stats)
    print("Total allocated size: %.1f KiB" % (total / 1024))
