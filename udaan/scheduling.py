"""Local weekday/time presets; no cron is exposed to the operator."""
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator, model_validator

from udaan.contracts import Strict

PRESETS = [('Run Now','now'), ('Once','once'), ('Every Morning','morning'),
           ('Every Afternoon','afternoon'), ('Every Evening','evening'), ('Daily','daily'),
           ('Twice Daily','twice_daily'), ('Three Times Daily','three_daily'),
           ('Weekdays','weekdays'), ('Weekends','weekends'), ('Weekly','weekly'), ('Custom','custom')]
DAYS = ['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday']
DEFAULT_TIMES = {'morning':['08:00'], 'afternoon':['14:00'], 'evening':['19:00'],
                 'twice_daily':['08:00','18:00'], 'three_daily':['08:00','14:00','20:00']}


class Timing(Strict):
    frequency: str = Field('daily', pattern='^(now|once|morning|afternoon|evening|daily|twice_daily|three_daily|weekdays|weekends|weekly|custom)$')
    timezone: str = 'UTC'
    times: list[str] = Field(default_factory=lambda:['08:00'], min_length=1, max_length=6)
    weekdays: list[int] = Field(default_factory=lambda:[0], min_length=1, max_length=7)

    @field_validator('timezone')
    @classmethod
    def zone(cls, value):
        try:
            ZoneInfo(value)
        except Exception as exc:
            raise ValueError('Choose a valid time zone') from exc
        return value

    @field_validator('times')
    @classmethod
    def clock_times(cls, values):
        import re
        if len(values) != len(set(values)) or any(not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', x) for x in values):
            raise ValueError('Use distinct times in HH:MM format')
        return sorted(values)

    @field_validator('weekdays')
    @classmethod
    def valid_days(cls, value):
        if len(set(value)) != len(value) or any(x not in range(7) for x in value):
            raise ValueError('Choose distinct weekdays, Monday through Sunday')
        return sorted(value)

    @model_validator(mode='after')
    def count(self):
        expected = 2 if self.frequency=='twice_daily' else 3 if self.frequency=='three_daily' else None if self.frequency=='custom' else 1
        if expected and len(self.times)!=expected:
            raise ValueError(f'This preset requires {expected} time(s)')
        if self.frequency=='weekly' and len(self.weekdays)!=1:
            raise ValueError('Weekly requires one weekday')
        return self

    def days(self):
        return ([0,1,2,3,4] if self.frequency=='weekdays' else [5,6] if self.frequency=='weekends'
                else self.weekdays if self.frequency in {'weekly','custom'} else list(range(7)))


def occurrences(rule, around, *, previous=False):
    rule = rule if isinstance(rule, Timing) else Timing.model_validate(rule)
    zone = ZoneInfo(rule.timezone)
    local = around.astimezone(zone)
    for offset in range(9):
        day = local.date() + timedelta(days=-offset if previous else offset)
        if day.weekday() not in rule.days():
            continue
        for value in sorted(rule.times, reverse=previous):
            naive = datetime.combine(day, time.fromisoformat(value))
            candidate = naive.replace(tzinfo=zone, fold=0).astimezone(timezone.utc)
            # Skip nonexistent local times; ambiguous times run once at their first occurrence.
            if candidate.astimezone(zone).replace(tzinfo=None) != naive:
                continue
            if (candidate <= around) if previous else (candidate > around):
                return candidate
    raise ValueError('No schedule occurrence in the bounded week')


def describe(rule):
    rule = rule if isinstance(rule, Timing) else Timing.model_validate(rule)
    name = dict((v,k) for k,v in PRESETS)[rule.frequency]
    days = ', '.join(DAYS[x] for x in rule.weekdays) if rule.frequency in {'weekly','custom'} else ''
    return f"{name}{' on ' + days if days else ''} at {', '.join(rule.times)} ({rule.timezone})."
