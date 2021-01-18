import fastapi
import datetime
import linecache
import pathlib
import objgraph
import gc

import mlrun
import time
import mlrun.errors
import mlrun.api.utils.periodic
import tracemalloc

from mlrun.utils import logger

router = fastapi.APIRouter()


@router.post("/monitoring/memory/enable")
async def enable_memory_monitoring(
    interval: int = mlrun.mlconf.httpdb.monitoring.memory.interval,
    number_of_frames: int = mlrun.mlconf.httpdb.monitoring.memory.number_of_frames,
):
    logger.info(
        "Enabling memory monitoring",
        interval=interval,
        number_of_frames=number_of_frames,
    )
    tracemalloc.start(number_of_frames)
    # mlrun.api.utils.periodic.run_function_periodically(interval, _run_memory_monitoring)
    mlrun.api.utils.periodic.run_function_periodically(
        interval, _generate_memory_sample
    )


@router.post("/monitoring/memory/sample")
def sample_memory_monitoring(
    object_type: str = None,
    start_index: int = None,
    sample_size: int = None,
    create_graph: bool = False,
    max_depth: int = 3,
):
    _generate_memory_sample(
        object_type, start_index, sample_size, create_graph, max_depth
    )


def _generate_memory_sample(
    object_type: str = None,
    start_index: int = None,
    sample_size: int = None,
    create_graph: bool = False,
    max_depth: int = 3,
):
    logger.debug("Generating memory sample")
    gc.collect()
    if object_type is not None and (
        start_index is not None or (sample_size is not None and sample_size > 0)
    ):

        requested_objects = objgraph.by_type(object_type)
        sample_size = sample_size or 1

        # If 'start_index' not given use 'sample_size' to calculated it from the end of the list
        if start_index is None or start_index < 0:
            start_index = len(requested_objects) - sample_size

        if start_index < len(requested_objects):

            # Iterate until 'sample_size' or the end of the list is reached
            for object_index in range(
                start_index, min(start_index + sample_size, len(requested_objects))
            ):
                logger.debug(
                    "Requested object",
                    object_type=object_type,
                    object_index=object_index,
                    total_objects=len(requested_objects),
                    requested_object=str(requested_objects[object_index])[:10000],
                )

                if create_graph:
                    logger.debug(
                        "Creating reference graph",
                        object_index=object_index,
                        max_depth=max_depth,
                    )
                    _create_object_ref_graph(
                        object_type,
                        requested_objects[object_index],
                        object_index,
                        max_depth=max_depth,
                    )

        else:
            message = "Object start index is invalid"
            logger.warn(
                message,
                object_type=object_type,
                start_index=start_index,
                total_objects=len(requested_objects),
            )
            raise mlrun.errors.MLRunBadRequestError(message)
    else:
        logger.debug(
            "Most common objects", most_common=str(objgraph.most_common_types())
        )


def _create_object_ref_graph(object_type, object_, object_index, max_depth=3):
    datetime_string = datetime.datetime.fromtimestamp(time.time()).strftime(
        "%Y-%m-%d_%H_%M_%S"
    )
    filename = f"{object_type}-{object_index}-{datetime_string}.dot"
    object_ref_graphs_dir = pathlib.Path("/mlrun/db/object-ref-graphs")
    if not object_ref_graphs_dir.exists():
        object_ref_graphs_dir.mkdir()
    objgraph.show_backrefs(
        object_,
        filename=str(object_ref_graphs_dir / filename),
        refcounts=True,
        max_depth=max_depth,
    )


def _run_memory_monitoring():
    logger.debug(
        "Taking memory snapshot",
        tracemalloc_memory=tracemalloc.get_tracemalloc_memory(),
    )
    now = datetime.datetime.utcnow().isoformat()
    snapshots_dir = pathlib.Path("/mlrun/db/memory-snapshots")
    if not snapshots_dir.exists():
        snapshots_dir.mkdir()
    filename = f"{str(snapshots_dir)}/memory-snapshot-{now}"
    snapshot = tracemalloc.take_snapshot()
    snapshot.dump(filename)
    display_top(snapshot, key_type="lineno")
    display_top(snapshot, key_type="filename")
    display_top(snapshot, key_type="traceback")
    display_top(snapshot, key_type="traceback", limit=1)


def display_top(snapshot, key_type="lineno", limit=10):
    top_stats = snapshot.statistics(key_type)
    if limit == 1 and key_type == "traceback":
        stat = top_stats[0]
        logger.debug(
            "Printing largest memory block",
            key_type=key_type,
            limit=limit,
            count=stat.count,
            size=stat.size,
        )
        for line in stat.traceback.format():
            logger.debug(line)
    else:
        logger.debug("Printing top memory blocks", key_type=key_type, limit=limit)
        for stat in top_stats[:10]:
            logger.debug(str(stat))


def display_top_2(snapshot, key_type="lineno", limit=10):
    # snapshot = snapshot.filter_traces(
    #     (
    #         tracemalloc.Filter(False, "<frozen importlib._bootstrap>"),
    #         tracemalloc.Filter(False, "<unknown>"),
    #     )
    # )
    top_stats = snapshot.statistics(key_type)
    now = datetime.datetime.utcnow().isoformat()
    print(f"Top {limit} lines by {key_type} - {now}")
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
