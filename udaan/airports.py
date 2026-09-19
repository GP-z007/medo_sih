"""Reference metadata enrichment, never a source airport list or route generator."""
import csv
import hashlib
from collections import defaultdict
from pathlib import Path

from udaan.db import AirportReference, now


def import_reference(db, airports: Path, regions: Path, reference_url: str):
    with regions.open(encoding='utf-8-sig') as stream:
        regions_by_code={r['code']:r['name'] for r in csv.DictReader(stream)}
    candidates=defaultdict(list)
    with airports.open(encoding='utf-8-sig') as stream:
        for row in csv.DictReader(stream):
            code=row.get('iata_code','')
            if len(code)==3 and code.isalpha() and code.isupper() and row.get('type')!='closed':
                candidates[code].append(row)
    digest=hashlib.sha256(airports.read_bytes()+regions.read_bytes()).hexdigest()
    imported=ambiguous=0
    for code,rows in candidates.items():
        if len(rows)!=1:
            ambiguous+=1
            continue
        row=rows[0]
        item=db.get(AirportReference,code)
        if item is None:
            item=AirportReference(iata=code)
            db.add(item)
        item.airport_name=row['name']
        item.city=row.get('municipality') or None
        item.state=regions_by_code.get(row.get('iso_region'))
        item.country=row['iso_country']
        item.timezone='Asia/Kolkata' if item.country=='IN' else None
        item.reference_url=reference_url
        item.reference_checksum=digest
        item.imported_at=now()
        imported+=1
    return {'imported':imported,'ambiguous_skipped':ambiguous,'checksum':digest}
