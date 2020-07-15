import asyncio
from typing import Any, Callable, List, Tuple, Dict, Union, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger as APSchedulerCronTrigger
from sqlalchemy.orm import Session

from mlrun.api import schemas
from mlrun.api.utils.singletons.db import get_db
from mlrun.utils import logger


class Scheduler:

    def __init__(self):
        self._scheduler = AsyncIOScheduler()
        # this should be something that does not make any sense to be inside project name or job name
        self._job_id_separator = "-_-"

    async def start(self, db_session: Session):
        logger.info('Starting scheduler')
        self._scheduler.start()
        # the scheduler shutdown and start operation are not fully async compatible yet -
        # https://github.com/agronholm/apscheduler/issues/360 - this sleep make them work
        await asyncio.sleep(0)

        # don't fail the start on re-scheduling failure
        try:
            self._reload_schedules(db_session)
        except Exception as exc:
            logger.warning('Failed reloading schedules', exc=exc)

    async def stop(self):
        logger.info('Stopping scheduler')
        self._scheduler.shutdown()
        # the scheduler shutdown and start operation are not fully async compatible yet -
        # https://github.com/agronholm/apscheduler/issues/360 - this sleep make them work
        await asyncio.sleep(0)

    def create_schedule(
        self,
        db_session: Session,
        project: str,
        name: str,
        kind: schemas.ScheduledObjectKinds,
        scheduled_object: Union[Dict, Callable],
        cron_trigger: schemas.ScheduleCronTrigger,
    ):
        logger.debug(
            'Creating schedule',
            project=project,
            name=name,
            kind=kind,
            scheduled_object=scheduled_object,
            cron_trigger=cron_trigger,
        )
        get_db().create_schedule(
            db_session, project, name, kind, scheduled_object, cron_trigger
        )
        self._create_schedule_in_scheduler(
            db_session, project, name, kind, scheduled_object, cron_trigger
        )

    def get_schedules(
        self, db_session: Session, project: str = None, kind: str = None
    ) -> schemas.SchedulesOutput:
        logger.debug('Getting schedules', project=project, kind=kind)
        db_schedules = get_db().get_schedules(db_session, project, kind)
        schedules = []
        for db_schedule in db_schedules:
            schedule = self._transform_db_schedule_to_schedule(db_schedule)
            schedules.append(schedule)
        return schemas.SchedulesOutput(schedules=schedules)

    def get_schedule(
        self, db_session: Session, project: str, name: str
    ) -> schemas.ScheduleOutput:
        logger.debug('Getting schedule', project=project, name=name)
        db_schedule = get_db().get_schedule(db_session, project, name)
        return self._transform_db_schedule_to_schedule(db_schedule)

    def delete_schedule(self, db_session: Session, project: str, name: str):
        logger.debug('Deleting schedule', project=project, name=name)
        job_id = self._resolve_job_id(project, name)
        self._scheduler.remove_job(job_id)
        get_db().delete_schedule(db_session, project, name)

    def _create_schedule_in_scheduler(
        self,
        db_session: Session,
        project: str,
        name: str,
        kind: schemas.ScheduledObjectKinds,
        scheduled_object: Any,
        cron_trigger: schemas.ScheduleCronTrigger,
    ):
        job_id = self._resolve_job_id(project, name)
        logger.debug('Adding schedule to scheduler', job_id=job_id)
        function, args, kwargs = self._resolve_job_function(
            db_session, kind, scheduled_object
        )
        self._scheduler.add_job(
            function,
            self.transform_schemas_cron_trigger_to_apscheduler_cron_trigger(
                cron_trigger
            ),
            args,
            kwargs,
            job_id,
        )

    def _reload_schedules(self, db_session: Session):
        logger.info('Reloading schedules')
        db_schedules = get_db().get_schedules(db_session)
        for db_schedule in db_schedules:
            # don't let one failure fail the rest
            try:
                self._create_schedule_in_scheduler(
                    db_session,
                    db_schedule.project,
                    db_schedule.name,
                    db_schedule.kind,
                    db_schedule.scheduled_object,
                    db_schedule.cron_trigger,
                )
            except Exception as exc:
                logger.warn('Failed rescheduling job. Continuing', exc=str(exc), db_schedule=db_schedule)

    def _transform_db_schedule_to_schedule(
        self, schedule_record: schemas.ScheduleRecord
    ) -> schemas.ScheduleOutput:
        job_id = self._resolve_job_id(schedule_record.project, schedule_record.name)
        job = self._scheduler.get_job(job_id)
        schedule = schemas.ScheduleOutput(**schedule_record.dict())
        schedule.next_run_time = job.next_run_time
        return schedule

    def _resolve_job_function(
        self,
        db_session: Session,
        scheduled_object_kind: schemas.ScheduledObjectKinds,
        scheduled_object: Any,
    ) -> Tuple[Callable, Optional[Union[List, Tuple]], Optional[Dict]]:
        """
        :return: a tuple (function, args, kwargs) to be used with the APScheduler.add_job
        """

        if scheduled_object_kind == schemas.ScheduledObjectKinds.job:
            # import here to avoid circular imports
            from mlrun.api.api.utils import submit

            return submit, [db_session, scheduled_object], {}
        if scheduled_object_kind == schemas.ScheduledObjectKinds.local_function:
            return scheduled_object, None, None

        # sanity
        message = "Scheduled object kind missing implementation"
        logger.warn(message, scheduled_object_kind=scheduled_object_kind)
        raise NotImplementedError(message)

    def _resolve_job_id(self, project, name) -> str:
        """
        :return: returns the identifier that will be used inside the APScheduler
        """
        return self._job_id_separator.join([project, name])

    @staticmethod
    def transform_schemas_cron_trigger_to_apscheduler_cron_trigger(
        cron_trigger: schemas.ScheduleCronTrigger,
    ):
        return APSchedulerCronTrigger(
            cron_trigger.year,
            cron_trigger.month,
            cron_trigger.day,
            cron_trigger.week,
            cron_trigger.day_of_week,
            cron_trigger.hour,
            cron_trigger.minute,
            cron_trigger.second,
            cron_trigger.start_date,
            cron_trigger.end_date,
            cron_trigger.timezone,
            cron_trigger.jitter,
        )
