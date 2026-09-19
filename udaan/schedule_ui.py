"""Small schedule form with local times and a live plain-language summary."""
from datetime import datetime, timedelta, timezone

from textual.widgets import Input, Select, Static

from udaan.scheduling import DAYS, DEFAULT_TIMES, PRESETS, Timing, describe
from udaan.tui import Form


class ScheduleForm(Form):
    def __init__(self, group, source_label, submit):
        self.group, self.source_label = group, source_label
        self.schedule_submit = submit
        fields = [
            ('frequency','When','daily',PRESETS),
            ('execute_at','Once: date/time (with UTC offset)',(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(timespec='minutes'),None),
            ('timezone','Time zone','UTC',None),
            ('time1','Time / First','08:00',None),
            ('time2','Second','18:00',None),
            ('time3','Third','20:00',None),
            ('weekday','Day','0',[(name,str(i)) for i,name in enumerate(DAYS)]),
            ('weekdays','Days (Mon,Tue,...)','Mon,Tue,Wed,Thu,Fri',None),
            ('times','Times (HH:MM,...)','08:00,18:00',None),
        ]
        super().__init__('Schedule', fields, self.save_schedule, save_label='SAVE SCHEDULE')

    def compose(self):
        yield from super().compose()

    async def on_mount(self):
        await self.query_one('.form-fields').mount(Static('',id='schedule-summary',markup=False))
        self.update_controls(reset=True)

    def values(self):
        return {key:self.query_one('#f-'+key, Select if choices is not None else Input).value
                for key, _, _, choices in self.fields}

    def payload(self, values):
        frequency = values['frequency']
        weekdays = ([int(values['weekday'])] if frequency=='weekly' else
                    [next(i for i,name in enumerate(DAYS) if name[:3].lower()==day.strip().lower()[:3])
                     for day in values['weekdays'].split(',')] if frequency=='custom' else [0])
        times = ([x.strip() for x in values['times'].split(',')] if frequency=='custom' else
                 [values['time1'], values['time2'], values['time3']][:3 if frequency=='three_daily' else 2 if frequency=='twice_daily' else 1])
        rule = Timing(frequency=frequency, timezone=values['timezone'], times=times, weekdays=weekdays)
        return {**self.group, **rule.model_dump(), 'execute_at':values['execute_at'] if frequency=='once' else None}

    def update_controls(self, reset=False):
        if not list(self.query('#f-frequency')):
            return
        frequency = self.query_one('#f-frequency',Select).value
        if reset:
            for i,value in enumerate(DEFAULT_TIMES.get(frequency,['08:00'])):
                self.query_one('#f-time'+str(i+1),Input).value=value
        visible = {'frequency','timezone'}
        if frequency=='once':
            visible.add('execute_at')
        elif frequency=='custom':
            visible.update({'weekdays','times'})
        elif frequency!='now':
            visible.add('time1')
            if frequency in {'twice_daily','three_daily'}:
                visible.add('time2')
            if frequency=='three_daily':
                visible.add('time3')
            if frequency=='weekly':
                visible.add('weekday')
        for key,_,_,choices in self.fields:
            widget=self.query_one('#f-'+key,Select if choices is not None else Input)
            widget.display=key in visible
            siblings=list(widget.parent.children)
            siblings[siblings.index(widget)-1].display=widget.display
        summaries=list(self.query('#schedule-summary'))
        if summaries:
            try:
                payload=self.payload(self.values())
                rule=Timing.model_validate({key:payload[key] for key in Timing.model_fields})
                search=self.group['search']
                windows='All Windows' if self.group['booking_windows']==[1,7,15,30,45] else ', '.join('T+'+str(x) for x in self.group['booking_windows'])
                mode={'Standard':'Normal','Conservative':'Careful','Robust':'Strong'}[search['resilience_profile']]
                when=('Run now.' if frequency=='now' else 'Once at '+payload['execute_at'] if frequency=='once' else describe(rule))
                summaries[0].update(f"This will scrape:\n{self.source_label} · {search['origin']} → {search['destination']}\n{windows} · {search['fare_scope']} fares · {mode} mode\nUp to {self.group['parallel_scrapes']} at once.\n\n{when}")
                self.query_one('#save').disabled=False
            except (ValueError,StopIteration,KeyError):
                summaries[0].update('Choose valid times, weekdays and a time zone to preview this schedule.')
                self.query_one('#save').disabled=True

    def update_custom_time(self):
        self.update_controls()

    def on_select_changed(self,event):
        self.update_controls(reset=event.select.id=='f-frequency')

    def on_input_changed(self,event):
        self.update_controls()

    async def save_schedule(self,values):
        await self.schedule_submit(self.payload(values))
