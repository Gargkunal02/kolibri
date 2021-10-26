from itertools import compress
from itertools import groupby

from django.db import connections
from django.db.models import Value
from django.db.models.functions import Greatest
from le_utils.constants import content_kinds
from morango.sync.backends.utils import calculate_max_sqlite_variables

from kolibri.core.logger.models import AttemptLog
from kolibri.core.logger.models import ContentSessionLog
from kolibri.core.logger.models import ContentSummaryLog
from kolibri.core.logger.models import MasteryLog


def _bulk_create(LogModel, logs):
    batch_size = (
        calculate_max_sqlite_variables() // len(LogModel._meta.fields)
        if connections[LogModel.objects.db].vendor == "sqlite"
        else 750
    )
    return LogModel.objects.bulk_create(logs, batch_size=batch_size)


def _update_session_log(log):
    ContentSessionLog.objects.filter(content_id=log.content_id, user=log.user).update(
        progress=Greatest("progress", Value(log.progress)),
        end_timestamp=Greatest("end_timestamp", Value(log.end_timestamp)),
    )


def _update_summary_log(log):
    ContentSummaryLog.objects.filter(content_id=log.content_id, user=log.user).update(
        progress=Greatest("progress", Value(log.progress)),
        end_timestamp=Greatest("end_timestamp", Value(log.end_timestamp)),
        completion_timestamp=Greatest(
            "completion_timestamp", Value(log.completion_timestamp)
        ),
    )


def _update_mastery_log(log):
    MasteryLog.objects.filter(summarylog_id=log.summarylog_id, user=log.user).update(
        complete=Greatest("complete", Value(log.complete)),
        end_timestamp=Greatest("end_timestamp", Value(log.end_timestamp)),
        completion_timestamp=Greatest(
            "completion_timestamp", Value(log.completion_timestamp)
        ),
    )


# Field that we want to update
# if there have been changes to an ExamAttemptLog
# when a migrated AttemptLog has already been created
attempt_log_fields_for_update = [
    "end_timestamp",
    "completion_timestamp",
    "time_spent",
    "complete",
    "correct",
    "hinted",
    "answer",
    "simple_answer",
    "interaction_history",
    "error",
]


def _update_attempt_logs(masterylog_id, logs):
    existing_logs = AttemptLog.objects.filter(masterylog_id=masterylog_id)
    existing_log_items = {log.item: log for log in existing_logs}
    to_create = []
    if existing_logs:
        sessionlog_id = existing_logs[0].sessionlog_id
    else:
        content_id = (
            MasteryLog.objects.filter(id=masterylog_id)
            .values_list("summarylog__content_id", flat=True)
            .first()
        )
        sessionlog_id = (
            ContentSessionLog.objects.filter(content_id=content_id)
            .values_list("id", flat=True)
            .first()
        )
    for log in logs:
        if log.item in existing_log_items:
            existing_log = existing_log_items[log.item]
            # Last write wins
            # Otherwise we ignore the updated log.
            # Need to cast the value for the unsaved log here, as otherwise
            # it is still in the string form, rather than datetime, as Morango
            # deserialize does not run to_python_value or from_db_value.
            if existing_log.end_timestamp < AttemptLog._meta.get_field(
                "end_timestamp"
            ).from_db_value(log.end_timestamp, None, None, None):
                for field in attempt_log_fields_for_update:
                    setattr(
                        existing_log,
                        field,
                        getattr(log, field, getattr(existing_log, field)),
                    )
                existing_log.save()
        else:
            log.sessionlog_id = sessionlog_id
            to_create.append(log)
    _bulk_create(AttemptLog, to_create)


def migrate_from_exam_logs(source_logs):  # noqa C901
    """
    This function performs a forward migration to generate logs of the following kinds:
    ContentSummaryLog
    ContentSessionLog
    MasteryLog
    AttemptLog

    to encode data that has previously been stored in:
    ExamLog
    ExamAttemptLog

    This is intended to be run to migrate historical data to the new format that
    should be used by Kolibri for logging user progress in quiz functionality.
    This serves two purposes:
    1. It allows user progress data about quizzes to be synced via single user syncing
    mechanisms. This was not possible for the ExamLog and ExamAttemptLog, due to the
    choice made about the partition used for that data, when the original intent for the
    'Exam' was made to be more summative and less formative, with a focus on timed classroom
    testing.
    2. It allows quizzes to be treated as a content type, which means that a quiz like experience
    can be exposed to a learner without coach intervention, and the same user progress tracking
    is used irrespective of whether it is a coach assigned quiz, or one discovered by the learner
    independently.

    Mapping:

    ExamLog ---------> MasteryLog + ContentSessionLog + ContentSummaryLog

    Fields:
    exam_id ---------> content_id (ContentSessionLog + ContentSummaryLog)
    user    ---------> user (ContentSessionLog + ContentSummaryLog)
    closed  ---------> complete (MasteryLog), progress=1 (ContentSessionLog + ContentSummaryLog)
    completion_timestamp ---------> completion_timestamp (MasteryLog + ContentSummaryLog),
                                    end_timestamp (MasteryLog + ContentSessionLog + ContentSummaryLog)

    ExamAttemptLog --> AttemptLog

    Fields:
    examlog ---------> masterylog
    content_id ------> combine with <item> as <content_id>:<item> to give a composite item id

    All other ExamAttemptLog fields are shared with AttemptLog

    For MasteryLog + ContentSessionLog + ContentSummaryLog
    infer start_timestamp from Min across ExamAttemptLogs

    mastery_criterion (MasteryLog), { "type": "quiz" }
    mastery_level (MasteryLog), short integer derived deterministically from exam_id
    channel_id (ContentSessionLog + ContentSummaryLog), None
    kind (ContentSessionLog + ContentSummaryLog), quiz
    """
    source_logs = source_logs.prefetch_related("attemptlogs")

    kind = content_kinds.QUIZ
    mastery_criterion = {"type": content_kinds.QUIZ}

    # ExamAttemptLog properties that we do not want
    # to copy onto the new AttemptLog
    exam_attempts_blocklist = {
        "id",
        "_morango_dirty_bit",
        "_morango_source_id",
        "_morango_partition",
        "examlog_id",
        "content_id",
    }

    i = 0

    BATCH_READ_SIZE = 750

    logs = source_logs[i : i + BATCH_READ_SIZE]

    while logs:
        content_session_logs = []
        content_summary_logs = []
        mastery_logs = []
        attempt_logs = []
        summary_log_ids = []
        for examlog in logs:
            examattemptlogs = examlog.attemptlogs.all()
            start_timestamp = min(e.start_timestamp for e in examattemptlogs)
            content_id = examlog.exam_id
            user = examlog.user
            complete = examlog.closed
            progress = 1 if examlog.closed else 0
            completion_timestamp = examlog.completion_timestamp
            end_timestamp = examlog.completion_timestamp
            dataset_id = user.dataset_id
            session_log = ContentSessionLog(
                user=user,
                content_id=content_id,
                start_timestamp=start_timestamp,
                end_timestamp=end_timestamp,
                progress=progress,
                kind=kind,
                dataset_id=dataset_id,
            )
            session_log.id = session_log.calculate_uuid()
            content_session_logs.append(session_log)

            summary_log = ContentSummaryLog(
                user=user,
                content_id=content_id,
                start_timestamp=start_timestamp,
                end_timestamp=end_timestamp,
                completion_timestamp=completion_timestamp,
                progress=progress,
                kind=kind,
                dataset_id=dataset_id,
            )
            summary_log.id = summary_log.calculate_uuid()
            content_summary_logs.append(summary_log)
            summary_log_ids.append(summary_log.id)

            mastery_log = MasteryLog(
                user=user,
                summarylog_id=summary_log.id,
                mastery_criterion=mastery_criterion,
                start_timestamp=start_timestamp,
                end_timestamp=end_timestamp,
                completion_timestamp=completion_timestamp,
                # Do this rather than just generating a random value
                # so that the mastery log id is deterministic.
                mastery_level=int(str(int(content_id, 16))[-9:]),
                complete=complete,
                dataset_id=dataset_id,
            )
            mastery_log.id = mastery_log.calculate_uuid()
            mastery_logs.append(mastery_log)

            for examattemptlog in examattemptlogs:
                data = examattemptlog.serialize()
                for f in exam_attempts_blocklist:
                    if f in data:
                        del data[f]
                data["sessionlog_id"] = session_log.id
                data["masterylog_id"] = mastery_log.id
                attemptlog = AttemptLog.deserialize(data)
                attemptlog.item = "{}:{}".format(
                    examattemptlog.content_id, attemptlog.item
                )
                attemptlog.id = attemptlog.calculate_uuid()
                attempt_logs.append(attemptlog)
        pre_existing_summary_logs = set(
            ContentSummaryLog.objects.filter(id__in=summary_log_ids).values_list(
                "id", flat=True
            )
        )
        mask = [log.id not in pre_existing_summary_logs for log in content_summary_logs]
        inverse_mask = [not m for m in mask]
        _bulk_create(ContentSessionLog, compress(content_session_logs, mask))
        _bulk_create(ContentSummaryLog, compress(content_summary_logs, mask))

        masked_masterylogs = list(compress(mastery_logs, mask))
        written_masterylog_ids = set(m.id for m in masked_masterylogs)
        _bulk_create(MasteryLog, masked_masterylogs)
        _bulk_create(
            AttemptLog,
            filter(lambda x: x.masterylog_id in written_masterylog_ids, attempt_logs),
        )

        for log in compress(content_session_logs, inverse_mask):
            _update_session_log(log)
        for log in compress(content_summary_logs, inverse_mask):
            _update_summary_log(log)
        for log in compress(mastery_logs, inverse_mask):
            _update_mastery_log(log)
        for masterylog_id, logs in groupby(attempt_logs, lambda x: x.masterylog_id):
            if masterylog_id not in written_masterylog_ids:
                _update_attempt_logs(masterylog_id, logs)
        i += BATCH_READ_SIZE
        logs = source_logs[i : i + BATCH_READ_SIZE]