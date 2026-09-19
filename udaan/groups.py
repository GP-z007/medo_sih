"""One durable group per action/occurrence, with honest rolling departure dates."""
from datetime import datetime, timedelta

from pydantic import Field, field_validator, model_validator
from sqlalchemy import select, text

from udaan.contracts import DomainError, JobInput
from udaan.db import Job, ScrapeGroup, now
from udaan.services import checksum, create_job, serialize


class GroupInput(JobInput):
    source_id: str | None = None
    source_ids: list[str] = Field(default_factory=list, max_length=5)
    all_sources: bool = False
    parallel_scrapes: int = Field(3, ge=1, le=5)
    network_profile: str = Field("automatic", pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    booking_windows: list[int] = Field(default_factory=lambda: [1,7,15,30,45], min_length=1, max_length=5)
    idempotency_key: str | None = Field(None, min_length=1, max_length=80)

    @field_validator('booking_windows')
    @classmethod
    def unique_windows(cls, values):
        if len(values) != len(set(values)) or any(not 1 <= value <= 366 for value in values):
            raise ValueError('Choose distinct T+1, T+7, T+15, T+30 or T+45 windows')
        return sorted(values)

    @model_validator(mode='after')
    def honest_dates(self):
        if sum([bool(self.source_id), bool(self.source_ids), self.all_sources]) != 1:
            raise ValueError('Choose one source, selected sources, or All Sources')
        if len(self.source_ids) != len(set(self.source_ids)):
            raise ValueError('Choose distinct sources')
        if not self.search.departure_date and set(self.booking_windows) - {1,7,15,30,45}:
            raise ValueError('Rolling windows must be T+1, T+7, T+15, T+30 or T+45')
        if self.search.departure_date and len(self.booking_windows) > 1:
            raise ValueError('All Windows uses departure dates relative to collection day; a fixed travel date is one search')
        return self


class GroupScheduleInput(GroupInput):
    execute_at: datetime | None = None
    frequency: str = 'once'
    timezone: str = 'UTC'
    times: list[str] | None = None
    weekdays: list[int] = Field(default_factory=lambda:[0])

    @field_validator('execute_at')
    @classmethod
    def timezone_required(cls, value):
        if value and value.tzinfo is None:
            raise ValueError('Choose an execution time with timezone')
        return value

    @model_validator(mode='after')
    def timing_valid(self):
        self.rule()
        if self.frequency in {'once'} and self.execute_at is None:
            raise ValueError('Once requires a date and time')
        if self.frequency not in {'once','now'} and self.search.departure_date:
            raise ValueError('Recurring schedules need rolling booking windows')
        return self

    def rule(self):
        from udaan.scheduling import DEFAULT_TIMES, Timing
        times = self.times
        if times is None:
            from zoneinfo import ZoneInfo
            times = ([self.execute_at.astimezone(ZoneInfo(self.timezone)).strftime('%H:%M')]
                     if self.execute_at else DEFAULT_TIMES.get(self.frequency, ['08:00']))
        return Timing(frequency=self.frequency, timezone=self.timezone, times=times, weekdays=self.weekdays)


def eligible_sources(db, data):
    from udaan.db import Source
    from udaan.services import gate, get
    if data.all_sources:
        sources = list(db.scalars(select(Source).where(Source.archived.is_(False)).order_by(Source.slug).with_for_update()))
    elif data.source_ids:
        sources = [get(db, Source, key, lock=True) for key in sorted(data.source_ids)]
    else:
        sources = [get(db, Source, data.source_id, lock=True)]
    ready, skipped = [], []
    for source in sources:
        try:
            recipe = gate(db, source)
            from udaan.discovery import require_route
            require_route(db, source, recipe, data.search)
            if (data.all_sources or data.source_ids) and source.status == 'BLOCKED':
                raise DomainError('SOURCE_BLOCKED', 'Source is paused after an access barrier; its proven recipe remains READY')
            if source.cooldown_until and source.cooldown_until > now():
                raise DomainError('SOURCE_COOLDOWN', 'Source is backing off until ' + source.cooldown_until.isoformat())
            ready.append(source)
        except DomainError as exc:
            if data.source_id:
                raise
            skipped.append({'source_id':source.id, 'source':source.name, 'state':'SKIPPED', 'reason':exc.message, 'code':exc.code})
    return ready, skipped


def create_group(db, data: GroupInput, *, schedule_id=None, scheduled_for=None):
    # One lock covers idempotency even when the group spans source rows.
    db.execute(text('SELECT pg_advisory_xact_lock(82422)'))
    payload = data.model_dump(mode='json', exclude={'idempotency_key'})
    if data.idempotency_key:
        existing = db.scalar(select(ScrapeGroup).where(ScrapeGroup.idempotency_key == data.idempotency_key))
        if existing:
            if checksum(existing.request) != checksum(payload):
                raise DomainError('IDEMPOTENCY_CONFLICT', 'This action key belongs to another scrape')
            return existing
    from udaan.global_ip import GlobalIPDataset
    GlobalIPDataset().get(data.network_profile)
    sources, skipped = eligible_sources(db, data)
    group = ScrapeGroup(source_id=data.source_id, request=payload, skipped=skipped, idempotency_key=data.idempotency_key,
                        schedule_id=schedule_id, scheduled_for=scheduled_for)
    db.add(group)
    db.flush()
    instant = now()
    # Interleave websites before moving to the next booking window.
    for window in data.booking_windows:
        departure = data.search.departure_date or instant.date() + timedelta(days=window)
        search = data.search.model_copy(update={'booking_window':window,'departure_date':departure})
        for source in sources:
            create_job(db, JobInput(source_id=source.id, search=search), group_id=group.id,
                       group_window=window, network_profile=data.network_profile,
                       schedule_id=schedule_id, scheduled_for=scheduled_for)
    return group


def create_group_schedule(db, data):
    from udaan.db import Schedule
    from udaan.scheduling import occurrences
    group_data = GroupInput.model_validate({key:getattr(data,key) for key in GroupInput.model_fields})
    eligible_sources(db, group_data)
    rule = data.rule()
    instant = now()
    when = (instant if data.frequency=='now' else data.execute_at if data.frequency=='once'
            else data.execute_at or occurrences(rule, instant))
    if data.frequency=='once' and when <= instant:
        raise DomainError('INVALID_SCHEDULE', 'Choose a future date and time', 422)
    schedule = Schedule(source_id=data.source_id, request=data.search.model_dump(mode='json'),
        booking_windows=data.booking_windows, group_request=group_data.model_dump(mode='json'),
        timing=rule.model_dump(mode='json'), next_run_at=when, enabled=True)
    db.add(schedule)
    db.flush()
    return schedule


def group_detail(db, group):
    from udaan.db import Source
    jobs = db.scalars(select(Job).join(Source, Job.source_id == Source.id)
                      .where(Job.group_id == group.id)
                      .order_by(Job.group_window, Source.name, Job.created_at, Job.id)).all()
    names = dict(db.execute(select(Source.id, Source.name)).all())
    children = [{**serialize(job), 'source':names[job.source_id]} for job in jobs]
    terminal = {'SUCCEEDED','FAILED','BLOCKED','CANCELLED'}
    return {**serialize(group), 'jobs':children, 'completed':sum(x['state'] in terminal for x in children),
            'succeeded':sum(x['state']=='SUCCEEDED' for x in children),
            'running':sum(x['state']=='RUNNING' for x in children),
            'waiting_for_you':sum(x['state']=='WAITING_FOR_OPERATOR' for x in children),
            'queued':sum(x['state']=='QUEUED' for x in children),
            'failed':sum(x['state'] in {'FAILED','BLOCKED','CANCELLED'} for x in children),
            'total':len(children), 'observation_count':sum(x['observation_count'] for x in children)}
