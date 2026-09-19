"""Supported-source configuration; every entry uses the normal registry."""
from sqlalchemy import select

from udaan.contracts import SourceInput
from udaan.db import Source
from udaan.services import add_source, validate_source

SUPPORTED_SOURCES = [
    ('IndiGo','indigo','https://www.goindigo.in/'),
    ('Air India','air-india','https://www.airindia.com/'),
    ('Air India Express','air-india-express','https://www.airindiaexpress.com/'),
    ('Akasa Air','akasa-air','https://www.akasaair.com/'),
    ('SpiceJet','spicejet','https://www.spicejet.com/'),

]


RETIRED_SEEDS = [
    ('Alliance Air','alliance-air','https://www.allianceair.in/'),
    ('Star Air','star-air','https://www.starair.in/'),
    ('FLY91','fly91','https://fly91.in/'),
    ('IndiaOne Air','indiaone-air','https://www.indiaoneair.com/'),
    ('MakeMyTrip','makemytrip','https://www.makemytrip.com/'),
    ('Goibibo','goibibo','https://www.goibibo.com/'),
    ('ixigo','ixigo','https://www.ixigo.com/'),
    ('EaseMyTrip','easemytrip','https://www.easemytrip.com/'),
    ('Cleartrip','cleartrip','https://www.cleartrip.com/'),
    ('Yatra','yatra','https://www.yatra.com/'),
    ('Booking.com','booking-com','https://www.booking.com/'),
]

SEED_NOTE = 'Operator requested public booking-interface collection; security barriers require manual action.'


def archive_retired_seeds(db):
    from udaan.db import Schedule
    for _, slug, url in RETIRED_SEEDS:
        source = db.scalar(select(Source).where(Source.slug == slug, Source.base_url == url))
        if source is None or source.configuration.get('notes') != SEED_NOTE:
            continue
        source.archived, source.enabled, source.status = True, False, 'ARCHIVED'
        for schedule in db.scalars(select(Schedule).where(Schedule.source_id == source.id)):
            schedule.enabled = False


def seed_sources(db):
    archive_retired_seeds(db)
    records=[]
    for name,slug,url in SUPPORTED_SOURCES:
        source=db.scalar(select(Source).where(Source.slug==slug))
        if source is None:
            source=add_source(db,SourceInput(name=name,slug=slug,base_url=url,enabled=False,source_type="airline",
                configuration={'permitted_collection':True,'notes':'Operator requested public booking-interface collection; security barriers require manual action.'}))
            source.status='NO_RECIPE'
        else:
            source.name = name
        source.configuration = {**source.configuration, 'recipe_contract_minimum': 3}
        if source.status != "BLOCKED":
            validate_source(db, source)
        records.append(source)
    return records
