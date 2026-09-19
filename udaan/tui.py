"""Compact terminal console. The API owns all operational state."""
import json
import re
from datetime import date, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, HorizontalScroll, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Collapsible, DataTable, Footer, Input, Label, Select, Static

from udaan.config import settings

PAGES = ["Dashboard", "Scrape", "Sources", "Data", "AI", "Logs", "Settings"]
MODES = [("Normal", "Standard"), ("Careful", "Conservative"), ("Strong", "Robust")]
LEGACY_DATA_COLUMNS = ["collected_at", "airline", "origin", "destination", "departure_date", "total_fare", "currency"]
DATA_COLUMNS = ['collection_timestamp','source_airline','origin_iata','destination_iata','departure_date','booking_lead_days','cabin_class','fare_family','displayed_fare','currency','scrape_status']
STATE = {"SUCCEEDED": "SUCCESS", "WAITING_FOR_OPERATOR": "NEEDS YOUR HELP", "QUEUED": "QUEUED",
         "RUNNING": "RUNNING", "BLOCKED": "STOPPED", "FAILED": "FAILED", "CANCELLED": "CANCELLED"}


def pretty(value):
    return json.dumps(value, indent=2, ensure_ascii=False, default=str)


def short_time(value):
    if not value:
        return "Never"
    return str(value).replace("T", " ").split(".")[0].replace("+00:00", "") + " UTC"


def simple_status(value):
    if value in {"NOT CONFIGURED", "UNCONFIGURED", "NOT TESTED", "NOT READY"}:
        return "NOT CONFIGURED" if value == "UNCONFIGURED" else value
    return "READY" if value in {"READY", "AVAILABLE", "CONNECTED"} else "CHECKING" if value == "CHECKING" else "OFFLINE"


def gauge(value):
    if value is None:
        return "··········  measuring / unavailable"
    filled = round(min(100, max(0, value)) / 10)
    return "█" * filled + "░" * (10 - filled) + f"  {value:.1f}%"


def job_message(job):
    state = job.get("state")
    if job.get("purpose") == "DISCOVERY" and state != "WAITING_FOR_OPERATOR":
        return job.get("reason") or job.get("checkpoint") or "Discovering airports and routes"
    if job.get("purpose") == "RECIPE_BUILD" and state != "WAITING_FOR_OPERATOR":
        if state == "SUCCEEDED":
            return ("CANDIDATE VALIDATED — tested and saved without replacing the active recipe."
                    if job.get("build_base_recipe_id") else "RECIPE READY — tested and saved. You can scrape this website.")
        if state in {"FAILED", "BLOCKED", "CANCELLED"}:
            wording = {"MODEL_UNAVAILABLE":"Cannot connect to the configured AI provider.",
                "MODEL_TIMEOUT":"AI took too long to build the recipe.",
                "MODEL_INVALID_RESPONSE":"AI replied, but its answer could not be read.",
                "MODEL_SCHEMA_ERROR":"AI replied, but did not provide a valid next step.",
                "MODEL_EMPTY_RESPONSE":"AI returned an empty answer.",
                "MODEL_CONTEXT_TOO_LARGE":"This page supplied too much information for AI."}
            return "RECIPE FAILED — " + wording.get(job.get("error_code"), job.get("reason") or "Open Advanced for the reason.")
        return "Building Recipe: " + ("Waiting for the browser…" if state == "QUEUED" else job.get("checkpoint", "Opening website") + "…")
    if state == "WAITING_FOR_OPERATOR":
        return "Udaan needs your help. Complete the step in the browser window."
    if state == "SUCCEEDED":
        count = job.get("observation_count", 0)
        return f"Saved {count} fares." if count else "Search finished. The website showed no flights."
    if state == "QUEUED":
        return "Waiting for the browser to become available."
    if state == "RUNNING":
        return {"search": "Searching the website…", "results": "Reading fares…"}.get(job.get("checkpoint"), job.get("checkpoint") or "Opening Udaan Browser…")
    return {"EXTRACTION_STRUCTURE_FAILED": "The website changed. Its scraping recipe needs fixing.",
            "EXTRACTION_SCHEMA_FAILED": "The fares could not be verified. No data was saved.",
            "OPERATOR_WAIT_TIMEOUT": "Stopped because the time allowed for help ran out.",
            "OPERATOR_CANCELLED": "You cancelled this scrape.",
            "BROWSER_SESSION_LOST": "The browser closed before the scrape finished.",
            "WORKER_SESSION_LOST": "The worker stopped. Retry to start a new scrape."}.get(job.get("error_code"), job.get("reason") or "Ready to scrape.")


def scrape_progress(item):
    if not item:
        return "No scrape running."
    if item.get("is_group"):
        group = item["group"]
        active = group["completed"] < group["total"]
        if not active:
            return "No scrape running."
        request = group["request"]
        scope = ("All Sources" if request.get("all_sources") else
                 f"{len(request.get('source_ids', []))} Sources" if request.get("source_ids") else
                 item.get("source", "Source"))
        search = request.get("search", {})
        lines = [f"{scope}     {search.get('origin', '—')} → {search.get('destination', '—')}", "",
                 f"Complete        {group['succeeded']} / {group['total']}",
                 f"Running         {group['running']}",
                 f"Waiting         {group['queued'] + group['waiting_for_you']}",
                 f"Failed          {group['failed']}", ""]
        ranked = sorted(group["jobs"], key=lambda job: (
            job["state"] not in {"RUNNING", "WAITING_FOR_OPERATOR"},
            job["state"] != "SUCCEEDED", job.get("source", ""), job.get("group_window") or 0))
        for job in ranked[:3]:
            symbol = "✓" if job["state"] == "SUCCEEDED" else "◐" if job["state"] == "RUNNING" else "!" if job["state"] == "WAITING_FOR_OPERATOR" else "○"
            state = (f"{job['observation_count']} fares" if job["state"] == "SUCCEEDED" else
                     "Needs your help" if job["state"] == "WAITING_FOR_OPERATOR" else
                     {"Reading fare options": "Reading prices", "search": "Searching"}.get(job.get("checkpoint"), job.get("checkpoint") or STATE.get(job["state"], job["state"]).title()))
            lines.append(f"{symbol} {job.get('source', 'Source'):<18} T+{job.get('group_window', '—'):<3}  {state}")
        return "\n".join(lines)
    if item.get("state") not in {"RUNNING", "QUEUED", "WAITING_FOR_OPERATOR"}:
        return "No scrape running."
    request = item.get("request", {})
    return (f"{item.get('source', 'Source')}     {request.get('origin', '—')} → {request.get('destination', '—')}\n\n"
            f"{job_message(item)}")


def event_message(item):
    detail, kind = item.get("details", {}), item.get("kind")
    return {"MODEL_REQUEST": "AI replied" if detail.get("response_validity") == "SCHEMA_VALID" else "AI request failed — see Advanced for its category", "PLANNER_ACTION": "Search action verified" if detail.get("verified") else "Search action needs another attempt", "MEMORY_RETRIEVED": "Looking at past fixes", "MEMORY_SAVED": "Eura saved useful page information", "RECIPE_BUILD_STAGE": detail.get("stage", "Building recipe"), "JOB_QUEUED": "Scrape queued", "JOB_STARTED": "Opening Udaan Browser",
            "BROWSER_STARTED": "Udaan Browser opened", "CHALLENGE_DETECTED": "Website needs your help",
            "CHALLENGE_RECHECKED": "Browser checked again", "JOB_RESUMED": "Browser ready; scraping continues",
            "JOB_SUCCEEDED": f"Saved {detail.get('observation_count', 0)} fares",
            "JOB_FAILED": "Scrape failed — open details", "JOB_BLOCKED": "Scrape stopped — open details",
            "JOB_CANCELLED": "Scrape cancelled", "CANCELLATION_REQUESTED": "Cancellation requested",
            "RECIPE_CHECKPOINT": {"search":"Searching the website", "results":"Reading fares"}.get(detail.get("checkpoint"), detail.get("checkpoint", "Checking data")),
            "STRUCTURE_DIAGNOSTIC": "Website information saved for recipe repair",
            "REPAIR_TRANSITION": {"GENERATING": "Reading page…", "MEMORY": "Looking at past fixes…", "ASKING_AI": "Asking AI…", "GENERATED": "Testing new recipe…", "STATIC_VALIDATION": "Testing new recipe…", "ISOLATED_TEST": "Testing new recipe…", "CONTRACT_VALIDATION": "Testing new recipe…", "LIVE_VALIDATION": "Testing new recipe on the website…", "APPROVED": "Saving recipe…", "PROMOTED": "New recipe ready."}.get(detail.get("state"), "Recipe repair: " + str(detail.get("state", "")).replace("_", " ").lower()),
            "REPAIR_MEMORY_WRITE_FAILED": "AI memory could not be updated"}.get(kind, "Scrape updated — open details")


class Form(ModalScreen):
    BINDINGS = [("escape", "dismiss", "Close")]

    def __init__(self, title, fields, submit, advanced=None, save_label="Save", diagnostics=None):
        super().__init__()
        self.title_text, self.fields, self.submit = title, fields, submit
        self.advanced, self.save_label = advanced or [], save_label
        self.diagnostics = diagnostics or []

    def widgets(self, fields):
        for key, label, value, options in fields:
            yield Label(label)
            if options is None:
                yield Input(str(value) if value is not None else "", id="f-" + key)
            else:
                yield Select(options, value=value if value is not None else Select.NULL,
                             allow_blank=value is None, id="f-" + key)

    def compose(self):
        with Vertical(classes="dialog form-dialog"):
            yield Label(self.title_text, classes="dialog-title")
            with VerticalScroll(classes="form-fields"):
                yield from self.widgets(self.fields)
                if self.advanced:
                    with Collapsible(title="Advanced", collapsed=True):
                        yield from self.widgets(self.advanced)
                if self.diagnostics:
                    with Collapsible(title="Advanced → Recipe compatibility", collapsed=True):
                        yield Static("\n".join(self.diagnostics), markup=False)
            yield Static("", id="form-error", markup=False)
            with Horizontal(classes="buttons"):
                yield Button(self.save_label, id="save", variant="primary")
                yield Button("Close", id="close")

    def on_mount(self):
        self.update_custom_time()

    def on_select_changed(self, event):
        if event.select.id == 'f-frequency':
            self.update_custom_time()

    def update_custom_time(self):
        choices = list(self.query('#f-frequency'))
        times = list(self.query('#f-execute_at'))
        if choices and times:
            control = times[0]
            visible = choices[0].value == 'custom'
            control.display = visible
            siblings = list(control.parent.children)
            siblings[siblings.index(control)-1].display = visible

    async def on_button_pressed(self, event):
        event.stop()
        if event.button.id == "close":
            self.dismiss()
        elif event.button.id == "save":
            values = {}
            for key, _, _, options in self.fields + self.advanced:
                widget = self.query_one("#f-" + key, Select if options is not None else Input)
                values[key] = None if widget.value is Select.NULL else widget.value
            event.button.disabled = True
            try:
                await self.submit(values)
            except Exception as exc:
                self.query_one("#form-error", Static).update(str(exc)[:800])
                event.button.disabled = False
                return
            self.dismiss()
            await self.app.refresh_page()


class Details(ModalScreen):
    BINDINGS = [("escape", "dismiss", "Close")]

    def __init__(self, title, value):
        super().__init__()
        self.heading, self.value = title, value

    def compose(self):
        with VerticalScroll(classes="dialog wide"):
            yield Label(self.heading, classes="dialog-title")
            if isinstance(self.value, str):
                yield Static(self.value, markup=False)
            else:
                value = self.value if isinstance(self.value, dict) else {"Items": len(self.value)}
                yield Static("\n".join(f"{key.replace('_', ' ').title()}: {v}" for key, v in value.items()
                    if isinstance(v, (str, int, float, bool)) and key not in {"id", "owner", "source_id", "lease_until", "checksum"}), markup=False)
                with Collapsible(title="Advanced → Raw", collapsed=True):
                    yield Static(pretty(self.value), markup=False)
            yield Button("Close", id="close")

    def on_button_pressed(self, event):
        event.stop()
        self.dismiss()


class SourceDetails(ModalScreen):
    BINDINGS = [("escape", "dismiss", "Close")]

    def __init__(self, source):
        super().__init__()
        self.source = source
        self.discovery = {}

    def compose(self):
        with Vertical(classes="dialog"):
            yield Label(self.source["name"] + " — Details")
            yield Static("Loading…", id="discovery-summary")
            with Horizontal():
                yield Button("Refresh Airports", id="refresh-airports")
                yield Button("Refresh Routes", id="refresh-routes")
                yield Button("Advanced", id="source-advanced")
                yield Button("Close", id="source-close")

    async def on_mount(self):
        try:
            self.discovery = await self.app.request("GET", f"/api/v1/sources/{self.source['id']}/discovery")
            d = self.discovery
            recipe = await self.app.request("GET", f"/api/v1/sources/{self.source['id']}/recipe-status")
            health = await self.app.request("GET", "/api/v1/health")
            run = d.get("latest_run") or {}
            self.query_one("#discovery-summary", Static).update(
                f"Website          {self.source['base_url']}\nStatus           {self.source['status'].replace('_', ' ')}\n"
                f"Last scrape      {short_time(d.get('last_scrape'))}\nLast error       {d.get('last_error') or 'None'}\n"
                f"Airports found   {d['airports']}\nRoutes found     {d['routes']}\n"
                f"Recipe           {recipe.get('version') or 'No working recipe'}\n"
                f"Recipe Status    {recipe['status']}\nEura             {health['services'].get('eura', {}).get('status', 'CHECKING')}\n"
                f"Last Test        {short_time(recipe.get('last_tested'))}\nBuilt By         {recipe.get('built_by') or '—'}\n"
                f"Last Repair      {short_time(recipe.get('last_repair'))}\n"
                f"Last checked     {short_time(d.get('last_checked'))}\n"
                f"Discovery        {run.get('state', 'NOT RUN')}\n"
                f"Complete         {'Yes' if run.get('complete') else 'No'}\n\n"
                + (run.get('reason') or 'Discovery must inspect the airline booking form.'))
            for identifier in ['refresh-airports', 'refresh-routes']:
                self.query_one('#' + identifier, Button).disabled = not d['discovery_supported']
        except Exception as exc:
            self.query_one("#discovery-summary", Static).update(str(exc))

    async def on_button_pressed(self, event):
        event.stop()
        if event.button.id == 'source-advanced':
            await self.app.push_screen(Details('Source — Advanced', {'source': self.source, 'discovery': self.discovery}))
            return
        if event.button.id == 'source-close':
            self.dismiss()
            return
        try:
            scope = 'AIRPORTS' if event.button.id == 'refresh-airports' else 'ROUTES'
            job = await self.app.request('POST', f"/api/v1/sources/{self.source['id']}/discovery",
                {'scope': scope, 'recipe_id': self.discovery.get('recipe_id')})
            self.query_one('#discovery-summary', Static).update('Discovery queued: ' + job['id'] + '\nFollow progress in Logs. The worker owns the browser.')
        except Exception as exc:
            self.query_one('#discovery-summary', Static).update(str(exc))


class JobDetails(ModalScreen):
    BINDINGS = [Binding("c", "operator('CONFIRM')", "Done — Check Again", priority=True),
                Binding("r", "operator('RECHECK')", "Check Again", priority=True),
                Binding("x", "operator('CANCEL')", "Cancel", priority=True),
                Binding("escape", "dismiss", "Close", priority=True)]

    def __init__(self, identifier):
        super().__init__()
        self.identifier, self.record = identifier, None

    def compose(self):
        with VerticalScroll(classes="dialog wide"):
            yield Label("Current Scrape", classes="dialog-title")
            yield Static("Checking your scrape…", id="job-summary", markup=False)
            with Horizontal(classes="buttons"):
                yield Button("Open Tab", id="OPEN_TAB")
                yield Button("Done — Check Again", id="CONFIRM", variant="primary")
                yield Button("Cancel", id="CANCEL", variant="error")
                yield Button("Close", id="close")
            yield Static("", id="operator-result", markup=False)
            yield Static("", id="readable-history", markup=False)
            with Horizontal(classes="buttons"):
                yield Button("Retry", id="job-retry")
            with Collapsible(title="Advanced → Raw", collapsed=True):
                yield Static("", id="job-history", markup=False)
        yield Footer()

    async def on_mount(self):
        await self.refresh_job()
        self.set_interval(1, self.refresh_job)

    async def refresh_job(self):
        try:
            job = self.record = await self.app.request("GET", f"/api/v1/jobs/{self.identifier}")
            request = job["request"]
            text = f"{job['source']}   {request.get('origin', 'Airports')} → {request.get('destination', 'Routes')}   {request.get('departure_date', '')}\n{STATE.get(job['state'], job['state'])}\n\n{job_message(job)}"
            waiting = job["state"] == "WAITING_FOR_OPERATOR"
            if waiting:
                text += f"\nBrowser: {job['browser_view_url']}\nThen choose Done — Check Again.\nTime remaining: {job['wait_remaining_seconds']:.0f}s\n{job.get('reason') or ''}"
            if job.get("purpose") == "RECIPE_BUILD":
                self.query_one(".dialog-title", Label).update("Building Recipe")
                stages = [e["details"]["stage"] for e in job.get("events", []) if e["kind"] == "RECIPE_BUILD_STAGE"]
                if stages:
                    text += "\n\n" + "\n".join(stages[-8:])
            self.query_one("#job-summary", Static).update(text)
            self.query_one("#CONFIRM", Button).display = waiting
            self.query_one("#OPEN_TAB", Button).display = waiting
            self.query_one("#CANCEL", Button).disabled = job["state"] in {"SUCCEEDED", "FAILED", "CANCELLED", "BLOCKED"}
            recipe_text = "Created" if job.get("recipe_id") else "Candidate only — not activated" if job.get("recipe_candidate") else "Not created"
            text += f"\n\nRecipe: {recipe_text}\nStarted: {short_time(job.get('started_at'))}\nFinished: {short_time(job.get('finished_at'))}"
            self.query_one("#job-summary", Static).update(text)
            self.query_one("#readable-history", Static).update("Events\n" + "\n".join(event_message(e) for e in job.get("events", [])[-12:] if e["kind"] != "STRUCTURE_DIAGNOSTIC"))
            self.query_one("#job-retry", Button).display = job["state"] in {"FAILED", "BLOCKED", "CANCELLED"}
            self.query_one("#job-history", Static).update(pretty(job))
        except Exception as exc:
            self.query_one("#operator-result", Static).update(str(exc))

    async def action_operator(self, action):
        if not self.record:
            return
        try:
            if self.record["state"] == "WAITING_FOR_OPERATOR":
                await self.app.request("POST", f"/api/v1/jobs/{self.identifier}/operator-action", {
                    "action": action, "challenge_id": self.record["challenge_id"], "idempotency_key": str(uuid4())})
                message = "Checking the browser…" if action != "CANCEL" else "Cancelling…"
            elif action == "CANCEL":
                await self.app.request("POST", f"/api/v1/jobs/{self.identifier}/cancel")
                message = "Cancelling…"
            else:
                return
            self.query_one("#operator-result", Static).update(message)
            await self.refresh_job()
        except Exception as exc:
            self.query_one("#operator-result", Static).update(str(exc))

    async def on_button_pressed(self, event):
        event.stop()
        if event.button.id == "close":
            self.dismiss()
        elif event.button.id == "job-retry":
            try:
                job = await self.app.request("POST", f"/api/v1/jobs/{self.identifier}/retry")
                self.identifier = self.app.current_job = job["id"]
                await self.refresh_job()
            except Exception as exc:
                self.query_one("#operator-result", Static).update(str(exc))
        else:
            await self.action_operator(event.button.id)


class PageInfo(ModalScreen):
    BINDINGS = [("escape", "dismiss", "Close"), ("i", "dismiss", "Close")]

    def __init__(self, title, message):
        super().__init__()
        self.heading, self.message = title, message

    def compose(self):
        with Vertical(classes="dialog info-dialog"):
            yield Label(self.heading, classes="dialog-title")
            yield Static(self.message)
            yield Button("Close", id="close")

    def on_button_pressed(self, event):
        event.stop()
        self.dismiss()


class GroupDetails(ModalScreen):
    BINDINGS = [("escape", "dismiss", "Close")]

    def __init__(self, identifier):
        super().__init__()
        self.identifier, self.children_jobs = identifier, []

    def compose(self):
        with Vertical(classes="dialog wide"):
            yield Label("Current Scrape", classes="dialog-title")
            yield Static("Loading windows…", id="group-progress", markup=False)
            yield DataTable(id="group-jobs", cursor_type="row")
            with Horizontal(classes="buttons"):
                yield Button("Export Group CSV", id="group-export-csv", variant="primary")
                yield Button("Close", id="close")

    async def on_mount(self):
        self.query_one(DataTable).add_columns("Source", "Window", "Travel Date", "Status", "Fare options")
        await self.refresh_group()
        self.set_interval(2, self.refresh_group)

    async def refresh_group(self):
        try:
            data = await self.app.request("GET", "/api/v1/scrape-groups/" + self.identifier)
            self.children_jobs = data['jobs']
            request = data['request']['search']
            source = 'All Sources' if data['request'].get('all_sources') else next((x['name'] for x in self.app.sources if x['id']==data['source_id']), 'Selected Sources')
            self.query_one('#group-progress', Static).update(
                f"{source} · {request['origin']} → {request['destination']}\n"
                f"{data['completed']} / {data['total']} complete · {data['observation_count']} fare options\n"
                f"Running {data['running']} · Queued {data['queued']} · Waiting for you {data['waiting_for_you']} · Failed {data['failed']}\n"
                + '\n'.join(x['source'] + ': SKIPPED — ' + x['reason'] for x in data.get('skipped', []))
                + "\nSelect a child for progress, Open Tab or human-action controls.")
            table = self.query_one(DataTable)
            row = table.cursor_row
            table.clear()
            for job in self.children_jobs:
                table.add_row(job['source'], f"T+{job['request']['booking_window']}", job['request']['departure_date'],
                    STATE.get(job['state'], job['state']), str(job['observation_count']))
            table.move_cursor(row=row)
        except Exception as exc:
            self.query_one('#group-progress', Static).update(str(exc))

    async def on_data_table_row_selected(self, event):
        event.stop()
        if self.children_jobs:
            await self.app.push_screen(JobDetails(self.children_jobs[event.cursor_row]['id']))

    async def on_button_pressed(self, event):
        event.stop()
        if event.button.id == "group-export-csv":
            try:
                result = await self.app.request("POST", f"/api/v1/scrape-groups/{self.identifier}/export", {"format": "csv"})
                await self.app.push_screen(ExportDetails(result["id"]))
            except Exception as exc:
                self.query_one("#group-progress", Static).update(str(exc))
        else:
            self.dismiss()


class ExportDetails(ModalScreen):
    BINDINGS = [("escape", "dismiss", "Close")]

    def __init__(self, identifier):
        super().__init__()
        self.identifier = identifier

    def compose(self):
        with Vertical(classes="dialog wide"):
            yield Label("Export", classes="dialog-title")
            yield Static("Saving your data…", id="export-result", markup=False)
            yield Button("Close", id="close")

    async def on_mount(self):
        await self.update_export()
        self.set_interval(1, self.update_export)

    async def update_export(self):
        try:
            records = await self.app.request("GET", "/api/v1/exports")
            item = next(x for x in records["items"] if x["id"] == self.identifier)
            text = "Saving your data…"
            if item["state"] == "SUCCEEDED":
                scope = (f" · {item.get('matched_source_count', 0)} sources · {item.get('matched_job_count', 0)} jobs"
                         if item.get('matched_source_count') is not None else '')
                text = (f"Export complete · {item['exported_count']} rows{scope}\n"
                        f"File: {Path(item['path']).name}\n"
                        f"Download: {settings().api_url}/api/v1/exports/{self.identifier}/download")
            elif item["state"] == "FAILED":
                text = "Could not save the export. " + (item.get("error") or "Try again.")
            self.query_one("#export-result", Static).update(text)
        except Exception as exc:
            self.query_one("#export-result", Static).update(str(exc))

    def on_button_pressed(self, event):
        event.stop()
        self.dismiss()


class UdaanApp(App):
    TITLE = "UDAAN"
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    Screen { background: #080c10; color: #c8d1d9; }
    #title { height: 3; color: #60d8a4; text-style: bold; }
    #nav { height: 3; border-bottom: solid #526271; }
    Button { height: 3; min-width: 8; padding: 0 1; border: round #465663; background: #080c10; color: #d1d9df; }
    Button:hover, Button:focus { background: #172c32; border: round #80d8be; }
    Button.-primary, Button.-active { color: #85f1bb; border: round #74c99d; background: #10261e; }
    #nav Button { width: 1fr; min-width: 7; border: none; padding: 0; }
    #main { padding: 0 1; }
    #page-heading { height: 3; }
    #page-info { width: 7; min-width: 7; padding: 0; }
    #heading { width: 1fr; height: 3; text-style: bold; color: #84d7bd; }
    #health-line { height: 1; color: #8cbeb5; }
    #dashboard-panels { height: 9; }
    .monitor { width: 1fr; height: 100%; border: round #536575; padding: 0 1; }
    #service-panel { width: 33; }
    #counts-panel { width: 23; }
    #form-area { height: auto; }
    #scrape-config { height: auto; border: round #536575; padding: 0 2; }
    .scrape-fields { grid-size: 4; grid-columns: 16 1fr 16 1fr; grid-rows: 3; height: auto; }
    .scrape-fields Label { height: 3; content-align: left middle; }
    .scrape-fields Input, .scrape-fields Select { height: 3; width: 1fr; border: round #465663; padding: 0 1; }
    #s-source { column-span: 3; }
    #scrape-buttons { height: 3; align-horizontal: center; }
    #scrape-buttons Button { margin: 0 1; }
    #scrape-progress { display: none; height: auto; min-height: 5; max-height: 13; border: round #536575; padding: 0 2; }
    .fields { grid-size: 4; grid-columns: 14 1fr 14 1fr; grid-rows: 3; height: auto; margin: 1 0; }
    .fields Input, .fields Select { height: 3; border: round #465663; padding: 0 1; }
    .fields Label { height: 3; content-align: left middle; }
    Input, Select { height: 3; margin: 0; border: round #465663; background: #0c1218; }
    Input:focus { border: round #75bda5; }
    #status { height: auto; max-height: 4; color: #c8d1d9; }
    #actions { height: 3; }
    #actions Button { margin-right: 1; }
    DataTable { height: 1fr; border: round #536575; background: #080c10; }
    DataTable > .datatable--header { background: #18232b; color: #78d6b1; text-style: bold; }
    DataTable > .datatable--cursor { background: #244039; color: #ffffff; }
    #ai-summary { height: auto; border: round #536575; padding: 0 1; }
    .dialog { width: 76; height: auto; max-height: 95%; padding: 0 1; background: #0c1218; border: round #80d8be; }
    .form-dialog { height: 90%; }
    .form-fields { height: 1fr; }
    .eura-field { height: 3; }
    .eura-field Label { width: 26; height: 3; content-align: left middle; }
    .eura-field Input { width: 1fr; }
    .dialog.wide { width: 95%; }
    .info-dialog { width: 60; max-width: 95%; }
    Select > SelectCurrent { height: 1; border: none; padding: 0; background: #0c1218; }
    Select:focus > SelectCurrent { border: none; }
    SelectCurrent > Static#label { height: 1; color: #c8d1d9; }
    SelectOverlay { background: #101820; color: #e1ebe7; border: round #80d8be; max-height: 12; }
    OptionList { background: #101820; color: #e1ebe7; }
    Select:focus, Select.-expanded { border: round #a2ffcc; }
    .dialog-title { height: 2; color: #85e5ba; text-style: bold; }
    ModalScreen { align: center middle; background: #00000080; }
    .buttons { height: 3; }
    .buttons Button { margin-right: 1; }
    Collapsible { padding: 0; margin: 0; border: none; background: #0c1218; }
    #form-error, #operator-result { height: auto; color: #e8b777; }
    #job-summary, #job-history, #export-result, #schedule-summary { height: auto; }
    Footer { background: #14221d; }
    .scrape-fields.narrow { grid-size: 2; grid-columns: 16 1fr; }
    .scrape-fields.narrow > #s-source, .scrape-fields.narrow > #s-window,
    .scrape-fields.narrow > #s-cabin, .scrape-fields.narrow > #s-mode,
    .scrape-fields.narrow > #s-network { column-span: 1; }
    """
    BINDINGS = [*[Binding(str(i + 1), f"page({i})", title, show=False) for i, title in enumerate(PAGES)],
                Binding("i", "page_info", "Info"), Binding("q", "quit", "Quit"), Binding("r", "refresh", "Refresh"),
                Binding("enter", "inspect", "Details"), Binding("?", "help", "Help")]

    def __init__(self, transport=None):
        super().__init__()
        cfg = settings()
        headers = {"Authorization": "Bearer " + cfg.api_token.get_secret_value()} if cfg.api_token else {}
        self.client = httpx.AsyncClient(base_url=cfg.api_url, headers=headers, timeout=10, transport=transport)
        self.page_index, self.items, self.sources, self.jobs = 0, [], [], []
        self.network_profiles = [{"id": "automatic", "label": "Automatic", "provider": "system-network"}]
        self.health = {}
        self.query_spec = {"dataset": "canonical", "columns": DATA_COLUMNS.copy(), "filters": [],
                           "sort": "collected_at", "descending": True, "limit": 100, "offset": 0}
        self.scrape_values = {"source": "all", "origin": "DEL", "destination": "BOM",
                              "date": "", "window": "all",
                              "cabin": "All Available", "passengers": "1", "mode": cfg.default_resilience_profile,
                              "network": "automatic", "parallel": "3"}
        self.data_values = {"source": "", "airline": "", "origin": "", "destination": "",
                            "from": "", "to": "", "scraped": "", "cabin": "", "family": "", "window": "", "rows": "100"}
        self.preferences_path = Path("var/ui-preferences.json")
        try:
            preferences = json.loads(self.preferences_path.read_text())
            if preferences.get("mode") in dict((value, name) for name, value in MODES):
                self.scrape_values["mode"] = preferences["mode"]
            if 1 <= int(preferences.get("rows", 100)) <= 1000:
                self.data_values["rows"] = str(preferences.get("rows", 100))
        except (OSError, ValueError, TypeError):
            pass
        self.advanced_filters = []
        self.current_job = None
        self.shown_challenges = set()
        self.refreshing, self.switching = False, False

    def compose(self) -> ComposeResult:
        # from udaan.branding import terminal_title
        # yield Static(terminal_title(), id="title")
        with Horizontal(id="nav"):
            for i, name in enumerate(PAGES):
                yield Button(f"{i + 1} {name}", id=f"nav-{i}")
        yield Static(" Checking services…", id="health-line", markup=False)
        with Vertical(id="main"):
            with Horizontal(id="page-heading"):
                yield Label("Dashboard", id="heading")
                yield Button(r"\[i]", id="page-info", tooltip="About this page (I)")
            with Horizontal(id="dashboard-panels"):
                yield Static("Checking services…", id="service-panel", classes="monitor", markup=False)
                yield Static("", id="counts-panel", classes="monitor", markup=False)
                yield Static("", id="system-panel", classes="monitor", markup=False)
            yield Vertical(id="form-area")
            yield Static("", id="ai-summary", markup=False)
            yield HorizontalScroll(id="actions")
            yield Static("No scrape running.", id="scrape-progress", markup=False)
            yield Static("", id="status", markup=False)
            yield DataTable(id="records", cursor_type="row", zebra_stripes=False)
        yield Footer()

    async def on_mount(self):
        for identifier, title in [("service-panel", " UDAAN "), ("counts-panel", " SCRAPING "), ("system-panel", " SYSTEM ")]:
            self.query_one("#" + identifier).border_title = title
        self.set_interval(3, self.refresh_page)
        self._activity_frame = 0
        self.set_interval(.4, self.animate_activity)
        await self.action_page(0)

    async def on_unmount(self):
        await self.client.aclose()

    async def request(self, method, path, data=None):
        try:
            response = await self.client.request(method, path, json=data)
        except httpx.HTTPError as exc:
            raise RuntimeError("Udaan API is offline. Run `udaan ser`, then press R.") from exc
        if response.is_error:
            try:
                error = response.json()
                message = {"RECIPE_MISSING": "This website needs a recipe. Choose Build with Eura AI.",
                           "SOURCE_NOT_VALIDATED": "Confirm collection permission in Edit → Advanced first.",
                           "BROWSER_UNAVAILABLE": "Browser View is not ready. Run udaan ser."}.get(error.get("error"), error.get("message") or "Please check the entered values.")
                if error.get("errors"):
                    message = "; ".join(x["message"] for x in error["errors"])
            except ValueError:
                message = f"Udaan API returned HTTP {response.status_code}."
            raise RuntimeError(message)
        return response.json()

    def field_widgets(self, prefix, fields):
        for key, label, value, options in fields:
            yield Label(label)
            if options is None:
                yield Input(str(value or ""), id=prefix + key)
            else:
                yield Select(options, value=value if value is not None else Select.NULL,
                             allow_blank=value is None, id=prefix + key)

    def remember_fields(self):
        prefix, target = ("s-", self.scrape_values) if self.page_index == 1 else ("d-", self.data_values)
        if self.page_index not in {1, 3}:
            return
        for widget in self.query("Input, Select"):
            if widget.id and widget.id.startswith(prefix):
                target[widget.id[2:]] = None if widget.value is Select.NULL else widget.value

    async def action_page(self, number):
        if number not in range(len(PAGES)) or self.switching:
            return
        self.switching = True
        self.remember_fields()
        self.page_index = number
        try:
            self.sources = (await self.request("GET", "/api/v1/sources"))["items"]
        except RuntimeError:
            pass
        if number == 1:
            try:
                self.network_profiles = (await self.request("GET", "/api/v1/network-profiles"))["profiles"]
            except RuntimeError:
                pass
        self.query_one("#heading", Label).update(PAGES[number].upper())
        for i in range(len(PAGES)):
            self.query_one(f"#nav-{i}", Button).set_class(i == number, "-active")
        self.query_one("#dashboard-panels").display = number == 0
        self.query_one("#ai-summary").display = number == 4
        self.query_one("#scrape-progress").display = number == 1
        self.query_one("#actions").display = number != 1
        self.query_one("#records", DataTable).border_title = " RECENT SCRAPES " if number == 1 else ""
        form = self.query_one("#form-area")
        await form.remove_children()
        if number == 1:
            v = self.scrape_values
            if v["source"] not in {"all", *[s["id"] for s in self.sources]}:
                v["source"] = next((s["id"] for s in self.sources if s["enabled"] and s.get("active_recipe_id") and s.get("configuration", {}).get("permitted_collection")), self.sources[0]["id"] if self.sources else None)
            cabins = next((source.get('cabins', []) for source in self.sources if source['id']==v['source']), [])
            if v['cabin'] not in ['All Available', *cabins]:
                v['cabin'] = 'All Available'
            ready_sources = [source for source in self.sources if source["enabled"] and source.get("status") == "READY"]
            if v["source"] != "all" and v["source"] not in {source["id"] for source in ready_sources}:
                v["source"] = "all"
            cabins = (next((source.get("cabins", []) for source in ready_sources if source["id"] == v["source"]), [])
                      if v["source"] != "all" else [])
            if v["cabin"] not in ["All Available", *cabins]:
                v["cabin"] = "All Available"
            fields = [("source", "Source", v["source"], [("All Sources", "all")] + [(source["name"], source["id"]) for source in ready_sources]),
                      ("origin", "From", v["origin"], None), ("destination", "To", v["destination"], None),
                      ("date", "Travel Date", v["date"], None), ("passengers", "Passengers", v["passengers"], None),
                      ("window", "Windows", v["window"], [("All Windows", "all")] + [(f"T+{n}", str(n)) for n in [1, 7, 15, 30, 45]] + [("Fixed travel date", "date")]),
                      ("cabin", "Cabin / Fare", v["cabin"], [(x, x) for x in ["All Available", *cabins]]),
                      ("mode", "Scraping Mode", v["mode"], MODES),
                      ("network", "Network", v["network"], [(x["label"], x["id"]) for x in self.network_profiles]),
                      ("parallel", "Parallel Jobs", v["parallel"], [(str(x), str(x)) for x in range(1, 6)])]
            panel = Vertical(
                Grid(*self.field_widgets("s-", fields), classes="scrape-fields narrow" if self.size.width <= 80 else "scrape-fields"),
                Horizontal(Button("SCRAPE NOW", id="act-scrape-now", variant="primary"),
                           Button("SCHEDULE", id="act-schedule"), id="scrape-buttons"),
                id="scrape-config")
            await form.mount(panel)
            panel.border_title = " SCRAPE "
            self.query_one("#s-date", Input).disabled = v["window"] != "date"
        elif number == 3:
            v = self.data_values
            fields = [("source", "Airline", v["source"], [("All", "")] + [(s["name"], s["id"]) for s in self.sources]),
                      ("origin", "From", v["origin"], None), ("destination", "To", v["destination"], None),
                      ("from", "Travel Date", v["from"], None), ("scraped", "Scrape Date (UTC)", v["scraped"], None),
                      ("cabin", "Cabin", v["cabin"], [("All", "")] + [(x,x) for x in ["Economy", "Premium Economy", "Business", "First"]]),
                      ("family", "Fare Type", v["family"], None),
                      ("window", "Booking Time", v["window"], [("All", "")] + [(f"T+{n}", str(n)) for n in [1, 7, 15, 30, 45]]),
                      ]
            await form.mount(Grid(*self.field_widgets("d-", fields), classes="fields"))
            await form.mount(Collapsible(Grid(*self.field_widgets('d-', [
                ('rows','Rows per page',v['rows'],None), ('to','Travel through (optional)',v['to'],None),
                ('airline','Operating airline',v['airline'],None)]), classes='fields'), title='Advanced', collapsed=True))
        actions = self.query_one("#actions")
        await actions.remove_children()
        labels = {
            0: [("go-scrape", "Scrape"), ("go-sources", "Add Source"), ("go-data", "See Data")],
            1: [],
            2: [("new-source", "Add"), ("recipe", "Recipe"), ("validate", "Test"), ("toggle", "On / Off"), ("edit-source", "Edit"), ("archive", "Archive")],
            3: [("show-data", "Show Data"), ("csv", "Export CSV"), ("parquet", "Export Parquet"), ("previous", "Previous"), ("next", "Next"), ("query", "Advanced Filters")],
            4: [("refresh-health", "Test AI"), ("model-settings", "Model Settings"), ("advanced-ai", "Advanced")],
            5: [("inspect", "Details")],
            6: [("preferences", "Edit"), ("model-settings", "AI"), ("advanced-settings", "Advanced"), ("official", "Official Data API")],
        }[number]
        await actions.mount(*(Button(label, id="act-" + key, variant="primary" if i == 0 else "default") for i, (key, label) in enumerate(labels)))
        self.switching = False
        await self.refresh_page()

    def animate_activity(self):
        if self.page_index != 1 or self.switching or isinstance(self.screen, ModalScreen):
            return
        jobs = getattr(self, "jobs", [])
        current = next((j for j in jobs if j["id"] == self.current_job), jobs[0] if jobs else None)
        if current and current["state"] == "RUNNING":
            self._activity_frame = (self._activity_frame + 1) % 4
            self.query_one("#status", Static).update("◐◓◑◒"[self._activity_frame] + " " + job_message(current))

    def fill_table(self, columns, items, rows=None):
        table = self.query_one("#records", DataTable)
        old_row = table.cursor_row
        table.clear(columns=True)
        table.add_columns(*columns)
        self.items = items
        for index, row in enumerate(rows if rows is not None else items):
            values = row if isinstance(row, (list, tuple)) else [row.get(key) for key in columns]
            table.add_row(*(pretty(v) if isinstance(v, (dict, list)) else str(v) if v is not None else "—" for v in values), key=str(index))
        if items:
            selected_id = getattr(self, "_select_source_id", None)
            selected_row = next((index for index, item in enumerate(items)
                                 if item.get("id") == selected_id), None)
            table.move_cursor(row=selected_row if selected_row is not None else min(old_row, len(items) - 1))
            if selected_row is not None:
                self._select_source_id = None

    async def refresh_page(self):
        if self.refreshing or self.switching or isinstance(self.screen, ModalScreen):
            return
        self.refreshing = True
        page = self.page_index
        try:
            health = self.health = await self.request("GET", "/api/v1/health")
            services = health["services"]
            statuses = {"Browser": simple_status(services.get("browser", {}).get("status")),
                        "Database": simple_status(services.get("postgresql", {}).get("status")),
                        "Eura": simple_status(services.get("eura", {}).get("status")),
                        "AI": simple_status(health["model"]["connectivity"]),
                        "AI Memory": simple_status(services.get("weaviate", {}).get("status")),
                        "Network": simple_status(services.get("network", {}).get("status")), "API": "READY"}
            self.query_one("#health-line", Static).update("  ".join(f"{key} {value}" for key, value in statuses.items()))
            status = self.query_one("#status", Static)
            if page in {0, 1, 5}:
                self.jobs = (await self.request("GET", "/api/v1/jobs"))["items"]
            if page == 0:
                stats = await self.request("GET", "/api/v1/dashboard")
                self.query_one("#service-panel", Static).update("\n".join(f"{key:<12} {value}" for key, value in statuses.items()) + "\nWorker       " + simple_status(services.get("worker", {}).get("status")))
                counts = stats["jobs"]
                self.query_one("#counts-panel", Static).update(f"Running       {counts.get('RUNNING', 0)}\nWaiting       {counts.get('WAITING_FOR_OPERATOR', 0)}\nSuccessful    {counts.get('SUCCEEDED', 0)}\nFailed        {counts.get('FAILED', 0) + counts.get('BLOCKED', 0)}\nQueued        {counts.get('QUEUED', 0)}\nSaved fares   {stats['observations']}")
                m = health["metrics"]
                memory = gauge(m.get("memory_percent")) if m.get("memory_percent") is not None else (f"{m['memory_used_bytes'] / 1024**3:.2f} GiB · no limit set" if m.get("memory_used_bytes") is not None else "unavailable")
                self.query_one("#system-panel", Static).update(f"CPU  {gauge(m.get('cpu_percent'))}\nRAM  {memory}\nDisk {gauge(m.get('disk_percent'))}\n\nCPU: one core = 100%\nRAM/CPU: container · Disk: filesystem")
                status.update("RECENT ACTIVITY" if self.jobs else "No scrapes yet. Add a source, then choose Scrape.")
                self.fill_table(["Time", "Source", "Route", "Status", "Fares"], self.jobs[:20],
                    [(short_time(x["created_at"]), x["source"], x["request"].get("origin", "Airports") + " → " + x["request"].get("destination", "Routes"), STATE.get(x["state"], x["state"]), x["observation_count"]) for x in self.jobs[:20]])
            elif page == 1:
                status.update("")
                grouped, seen = [], set()
                for job in self.jobs:
                    group_id = job.get('group_id')
                    if not group_id:
                        grouped.append(job)
                    elif group_id not in seen:
                        seen.add(group_id)
                        group = await self.request('GET', '/api/v1/scrape-groups/' + group_id)
                        grouped.append({**job, 'id':group_id, 'is_group':True, 'group':group})
                active = next((item for item in grouped if item.get("is_group") and item["group"]["completed"] < item["group"]["total"]), None)
                if active is None:
                    active = next((item for item in grouped if not item.get("is_group") and item["state"] in {"RUNNING", "QUEUED", "WAITING_FOR_OPERATOR"}), None)
                progress = self.query_one("#scrape-progress", Static)
                progress.border_title = " CURRENT SCRAPE "
                progress.update(scrape_progress(active))
                self.fill_table(["Airline", "Route", "Windows / Date", "Status", "Fares"], grouped,
                    [(('All Sources' if x['group']['request'].get('all_sources') else x['source']) if x.get('is_group') else x['source'], x['request'].get('origin','Airports')+' → '+x['request'].get('destination','Routes'),
                      ', '.join('T+'+str(w) for w in x['group']['request']['booking_windows']) if x.get('is_group') else x['request'].get('departure_date','—'),
                      f"{x['group']['completed']} / {x['group']['total']} complete" if x.get('is_group') else STATE.get(x['state'],x['state']),
                      x['group']['observation_count'] if x.get('is_group') else x['observation_count']) for x in grouped])
                waiting = next((x for x in self.jobs if x["state"] == "WAITING_FOR_OPERATOR" and x["challenge_id"] not in self.shown_challenges), None)
                if waiting:
                    self.shown_challenges.add(waiting["challenge_id"])
                    await self.push_screen(JobDetails(waiting["id"]))
            elif page == 2:
                self.sources = (await self.request("GET", "/api/v1/sources"))["items"]
                rows = []
                for source in self.sources:
                    label = source.get("status", "RECIPE_NOT_READY").replace("_", " ")
                    rows.append((source["name"], "ON" if source["enabled"] else "OFF", label, source.get("recipe_version") or "—", source["base_url"]))
                self.fill_table(["Source", "On / Off", "Status", "Recipe", "Website"], self.sources, rows)
                status.update("Select a website to edit, turn on or off, or choose its scraping recipe." if self.sources else "Add your first website with Add Source.")
            elif page == 3:
                result = await self.request("POST", "/api/v1/data/query", self.query_spec)
                rows = result["rows"]
                columns = list(rows[0]) if rows else self.query_spec["columns"]
                titles = {"collected_at": "Collected (UTC)", "airline": "Airline", "origin": "From", "destination": "To",
                          "departure_date": "Travel Date", "total_fare": "Fare", "currency": "Currency"}
                self.fill_table([titles.get(k, k.replace("_", " ").title()) for k in columns], rows,
                                [[row.get(k) for k in columns] for row in rows])
                status.update(f"{result['matched_count']} rows found · Showing {result['offset'] + 1 if rows else 0}–{result['offset'] + len(rows)} · Travel and scrape dates use separate filters" + ("\nNo matching data. Try another filter or run a scrape." if not rows else ""))
            elif page == 4:
                model = health["model"]
                memory = services.get("weaviate", {})
                latency = f"{model['latency_ms']:.0f} ms" if model["latency_ms"] is not None else "Not measured"
                from udaan.eura_ui import model_label, model_status
                recipe_ai = "WAITING FOR MODEL" if not model.get("configured") else "READY" if model["connectivity"] == "READY" and services.get("eura", {}).get("status") == "READY" and memory.get("status") == "READY" and services.get("sandbox", {}).get("status") == "AVAILABLE" else "NEEDS CHECK"
                self.query_one("#act-refresh-health", Button).disabled = not model.get("configured", False)
                self.query_one("#ai-summary", Static).update(f"AI Model       {model_status(model['connectivity'])}\nEura Engine    {statuses['Eura']}\nAI Memory      {simple_status(memory.get('status'))}\nRecipe AI      {recipe_ai}\n\nModel          {model_label(model['model'])}\nProvider       {model.get('provider') or 'OpenAI-compatible'}\nLast Check     {short_time(model['checked_at'])}\nResponse Time  {latency}\nLast Connected {short_time(model['last_successful_check'])}")
                status.update(model.get("last_error") or ("Add an AI provider in Settings to build recipes automatically." if not model.get("configured") else "Test AI checks a real response only when requested."))
                self.fill_table(["Service", "Latest Check"], [], [])
            elif page == 5:
                events = (await self.request("GET", "/api/v1/activity"))["items"]
                jobs = {x["id"]: x for x in self.jobs}
                self.fill_table(["Time", "Source", "Message"], events,
                    [(short_time(x["created_at"]), jobs.get(x["job_id"], {}).get("source", "Udaan"), event_message(x)) for x in events])
                status.update("Recent activity. Select a line for details." if events else "No activity yet. Run your first scrape.")
            else:
                cfg = await self.request("GET", "/api/v1/settings")
                rows = [("Scraping Mode", dict((value, name) for name, value in MODES).get(self.scrape_values["mode"], "Normal")),
                        ("Browser View", cfg["browser_view_url"]), ("Default Rows", self.data_values["rows"]),
                        ("AI Model", health["model"]["model"] or "Not configured"), ("AI Memory", statuses["AI Memory"]),
                        ("Global IP", services.get("global_ip", {}).get("status", "NOT CONFIGURED")),
                        ("Network", services.get("global_ip", {}).get("network", "SYSTEM DEFAULT")),
                        ("Egress capability", services.get("global_ip", {}).get("egress_capability", "NOT PROVIDED"))]
                self.fill_table(["Setting", "Value"], [{"setting": k, "value": v} for k, v in rows], rows)
                status.update("Normal settings here. Connection addresses and other technical options are in Advanced.")
        except Exception as exc:
            self.query_one("#status", Static).update(str(exc))
            if not self.health:
                self.query_one("#health-line", Static).update("API OFFLINE · Run `udaan ser`, then press R")
            self.fill_table(["Status"], [])
        finally:
            self.refreshing = False

    def selected(self):
        row = self.query_one("#records", DataTable).cursor_row
        if not self.items or row >= len(self.items):
            raise RuntimeError("Select a row first.")
        return self.items[row]

    async def action_inspect(self):
        try:
            item = self.selected()
            await self.push_screen(GroupDetails(item["id"]) if item.get("is_group") else JobDetails(item["id"]) if self.page_index in {0, 1} else SourceDetails(item) if self.page_index == 2 else Details("Details", item))
        except Exception as exc:
            self.notify(str(exc), severity="warning")

    async def action_refresh(self):
        await self.refresh_page()

    async def action_help(self):
        await self.push_screen(Details("Help", "1–7: switch pages · Tab: next field · Enter: open row · R: refresh · Q: quit\n\nAdd Source → Scrape → See Data\n\nIf the website needs help, complete the step in the browser, then choose Done — Check Again.\nClosing this console does not stop your scrapes."))

    async def on_data_table_row_selected(self, event):
        await self.action_inspect()

    async def on_button_pressed(self, event):
        identifier = event.button.id or ""
        if identifier == "page-info":
            await self.action_page_info()
        elif identifier.startswith("nav-"):
            await self.action_page(int(identifier[4:]))
        elif identifier.startswith("act-"):
            try:
                await self.perform_action(identifier[4:])
            except Exception as exc:
                self.query_one("#status", Static).update(str(exc))
                self.notify(str(exc), severity="warning")

    def on_select_changed(self, event):
        if event.select.id == 's-source' and not self.switching:
            cabins = next((source.get('cabins', []) for source in self.sources if source['id']==event.value), [])
            control = self.query_one('#s-cabin', Select)
            control.set_options([(x,x) for x in ['All Available', *cabins]])
            control.value = 'All Available'
        if event.select.id == "s-window" and event.value is not Select.NULL and not self.switching:
            control = self.query_one("#s-date", Input)
            control.disabled = event.value != 'date'
            if event.value == 'date' and not control.value:
                control.value = (date.today()+timedelta(days=30)).isoformat()

    def scrape_request(self):
        self.remember_fields()
        v = self.scrape_values
        source = next((s for s in self.sources if s["id"] == v["source"]), None)
        if not source and v["source"] != "all":
            raise ValueError("Add a source first.")
        if source and not source["enabled"]:
            raise ValueError("This source is off. Open Sources and choose Turn On / Off.")
        day = date.fromisoformat(v['date']) if v['window']=='date' else None
        windows = [1,7,15,30,45] if v['window']=='all' else [(day-date.today()).days if day else int(v['window'])]
        return {'source_id':source['id'] if source else None, 'all_sources':v['source']=='all',
            'parallel_scrapes':int(v['parallel']), 'network_profile':v['network'], 'booking_windows':windows,
            'search':{'origin':v['origin'].strip().upper(), 'destination':v['destination'].strip().upper(),
                'departure_date':day.isoformat() if day else None, 'booking_window':windows[0],
                'adults':int(v['passengers']), 'cabin':'Economy' if v['cabin']=='All Available' else v['cabin'],
                'fare_scope':'All Available' if v['cabin']=='All Available' else 'Selected Cabin',
                'resilience_profile':v['mode']}}

    def simple_query(self):
        self.remember_fields()
        v = self.data_values
        filters = list(self.advanced_filters)
        for key, column in [("source", "source_id"), ("airline", "airline"), ("origin", "origin"), ("destination", "destination")]:
            value = v[key].strip() if v[key] else ""
            if value:
                filters.append({"column": column, "value": value.upper() if key in {"origin", "destination"} else value})
        for key, op in [("from", "eq"), ("to", "lte")]:
            if v[key]:
                filters.append({"column": "departure_date", "op": op, "value": date.fromisoformat(v[key]).isoformat()})
        for key, column in [('scraped','collection_date'), ('cabin','cabin_class'), ('family','fare_family')]:
            if v[key]:
                value = date.fromisoformat(v[key]).isoformat() if key=='scraped' else v[key]
                filters.append({'column':column,'value':value})
        if v["window"]:
            filters.append({"column": "booking_lead_days", "value": int(v["window"])})
        self.query_spec = {**self.query_spec, "filters": filters, "limit": int(v["rows"]), "offset": 0}
        return self.query_spec

    async def perform_action(self, action):
        if action.startswith("go-"):
            await self.action_page({"go-scrape": 1, "go-sources": 2, "go-data": 3}[action])
            if action == "go-sources":
                await self.source_form()
        elif action == "inspect":
            await self.action_inspect()
        elif action in {"new-source", "edit-source"}:
            await self.source_form(self.selected() if action == "edit-source" else None)
        elif action == "toggle":
            record = self.selected()
            if not record["enabled"] and not record["configuration"].get("permitted_collection"):
                raise ValueError("Record collection permission in Edit → Advanced before turning this source on.")
            body = {key: record[key] for key in ["name", "slug", "base_url", "source_type", "configuration"]}
            body["enabled"] = not record["enabled"]
            await self.request("PUT", f"/api/v1/sources/{record['id']}", body)
            await self.refresh_page()
        elif action == "build-recipe":
            from udaan.eura_ui import RecipeBuild
            await self.push_screen(RecipeBuild(self.selected(), await self.request("GET", "/api/v1/health")))
        elif action == "validate":
            source = self.selected()
            summary = await self.request("GET", f"/api/v1/sources/{source['id']}/recipe-status")
            if not summary.get('recipe_id'):
                await self.recipe_form()
            else:
                await self.test_recipe(source, summary['recipe_id'])
        elif action == "archive":
            source = self.selected()
            async def archive(v):
                await self.request("POST", f"/api/v1/sources/{source['id']}/archive")
            await self.push_screen(Form("Archive " + source["name"] + "? Its history will be kept.", [], archive, save_label="Archive"))
        elif action == "recipe":
            await self.recipe_form()
        elif action in {"scrape-now", "schedule"}:
            from uuid import uuid4
            body = self.scrape_request()
            if action == 'schedule':
                from udaan.schedule_ui import ScheduleForm
                async def submit(payload):
                    await self.request('POST','/api/v1/group-schedules',payload)
                label = 'All Sources' if body['all_sources'] else next(x['name'] for x in self.sources if x['id']==body['source_id'])
                await self.push_screen(ScheduleForm(body,label,submit))
            else:
                group = await self.request('POST','/api/v1/scrape-groups', {**body,'idempotency_key':str(uuid4())})
                if group['jobs']:
                    self.current_job = group['jobs'][0]['id']
                await self.refresh_page()
                await self.push_screen(GroupDetails(group['id']))
        elif action == "retry":
            if self.selected().get('is_group'):
                await self.push_screen(GroupDetails(self.selected()['id']))
                return
            job = await self.request("POST", f"/api/v1/jobs/{self.selected()['id']}/retry")
            self.current_job = job["id"]
            await self.refresh_page()
        elif action == "schedules":
            records = (await self.request("GET", "/api/v1/schedules"))["items"]
            await self.push_screen(Details("Schedules", "\n".join(f"{'ON' if x['enabled'] else 'OFF'}  {short_time(x['next_run_at'])}  {x['request']['origin']} → {x['request']['destination']}" for x in records) or "No schedules yet."))
        elif action == "show-data":
            self.simple_query()
            await self.refresh_page()
        elif action in {"previous", "next"}:
            self.query_spec["offset"] = max(0, self.query_spec["offset"] + self.query_spec["limit"] * (1 if action == "next" else -1))
            await self.refresh_page()
        elif action in {"csv", "parquet"}:
            self.simple_query()
            export_query = {**self.query_spec, 'offset': 0}
            if export_query.get('dataset') == 'canonical' and export_query['columns'] == DATA_COLUMNS:
                from udaan.canonical import CANONICAL_COLUMNS
                export_query['columns'] = CANONICAL_COLUMNS
            result = await self.request("POST", "/api/v1/data/export", {"query": export_query, "format": action})
            await self.push_screen(ExportDetails(result["id"]))
        elif action == "query":
            await self.query_form()
        elif action == "refresh-health":
            await self.request("POST", "/api/v1/health/refresh")
            self.query_one("#status", Static).update("Testing AI and AI Memory…")
        elif action == "model-settings":
            from udaan.eura_ui import ModelSettings
            await self.push_screen(ModelSettings(await self.request("GET", "/api/v1/settings")))
        elif action == "advanced-ai":
            await self.push_screen(Details("AI — Advanced", self.health))
        elif action == "preferences":
            async def submit(v):
                if not 1 <= int(v["rows"]) <= 1000:
                    raise ValueError("Choose 1–1000 rows.")
                self.preferences_path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.preferences_path.with_suffix(".tmp")
                temporary.write_text(json.dumps({"mode": v["mode"], "rows": int(v["rows"])}))
                temporary.replace(self.preferences_path)
                self.scrape_values["mode"], self.data_values["rows"] = v["mode"], v["rows"]
            await self.push_screen(Form("Settings", [("mode", "Scraping Mode", self.scrape_values["mode"], MODES), ("rows", "Default Rows", self.data_values["rows"], None)], submit))
        elif action == "advanced-settings":
            await self.push_screen(Details("Advanced Settings", await self.request("GET", "/api/v1/settings")))
        elif action == "official":
            await self.push_screen(Details("Official Data API", "Future airline/government connection\n\nDEMONSTRATION ONLY · PRODUCTION PARTNERS: NONE\n\n" + pretty(await self.request("GET", "/institutional/v1/health"))))

    async def source_form(self, record=None):
        record = record or {}
        fields = [("name", "Name", record.get("name", ""), None), ("base_url", "Website", record.get("base_url", "https://"), None),
                  ("source_type", "Type", record.get("source_type", "airline"), [("Airline", "airline"), ("Travel website", "OTA"), ("Official data", "institutional"), ("Other", "other")])]
        advanced = [("slug", "Short name (leave blank for automatic)", record.get("slug", ""), None),
                    ("permitted", "Collection permission confirmed", "yes" if record.get("configuration", {}).get("permitted_collection") else "no", [("No", "no"), ("Yes", "yes")])]
        async def submit(v):
            permitted = v.pop("permitted") == "yes"
            v["slug"] = v["slug"] or re.sub(r"[^a-z0-9]+", "-", v["name"].casefold()).strip("-")
            v["configuration"] = {**record.get("configuration", {}), "permitted_collection": permitted}
            v["enabled"] = record.get("enabled", False)
            saved = await self.request("PUT" if record else "POST", "/api/v1/sources" + ("/" + record["id"] if record else ""), v)
            if not record:
                from udaan.eura_ui import RecipeDetails
                self._select_source_id = saved["source_id"]
                self.notify("Source added. Recipe not ready." if saved["created"] else
                            "Source already exists. Opening existing source…")
                self.call_after_refresh(lambda: self.push_screen(RecipeDetails(saved)))
        await self.push_screen(Form("Edit Source" if record else "Add Source", fields, submit, advanced=advanced, save_label="Save" if record else "Add Source"))

    async def recipe_form(self):
        from udaan.eura_ui import RecipeDetails
        await self.push_screen(RecipeDetails(self.selected()))

    async def test_recipe(self, source, recipe_id):
        async def submit(v):
            search = {"origin": v["origin"].strip().upper(), "destination": v["destination"].strip().upper(),
                      "departure_date": v["date"], "booking_window": (date.fromisoformat(v["date"]) - date.today()).days,
                      "adults": 1, "cabin": "Economy"}
            result = await self.request("POST", f"/api/v1/recipes/{recipe_id}/test", search)
            self.current_job = result["id"]
            self.call_after_refresh(lambda: self.push_screen(JobDetails(result["id"])))
        await self.push_screen(Form("Test Recipe — " + source['name'], [
            ("origin", "Test route — From", "DEL", None), ("destination", "To", "BOM", None),
            ("date", "Travel Date (one adult, Economy)", (date.today()+timedelta(days=30)).isoformat(), None)], submit, save_label="Test"))

    async def choose_recipe(self, source):
        records = (await self.request("GET", f"/api/v1/sources/{source['id']}/recipes"))["items"]
        usable = [r for r in records if r["selectable"]]
        diagnostics = [f"{r.get('manifest', {}).get('source_slug', 'Recipe')} {r['version']}: {r['compatibility_reason']}" for r in records]
        if not usable:
            await self.push_screen(Details("No working recipe", "Test a saved recipe or use Build with Eura AI."))
            return
        async def submit(v):
            await self.request("POST", f"/api/v1/sources/{source['id']}/recipe", {"recipe_id": v["recipe_id"]})
        await self.push_screen(Form("Select Recipe — " + source["name"],
            [("recipe_id", "Working recipe", usable[0]["id"], [(r["version"] + "  READY", r["id"]) for r in usable])], submit, save_label="Use Recipe", diagnostics=diagnostics))

    async def query_form(self):
        spec = self.query_spec
        extra = {x["column"]: x["value"] for x in self.advanced_filters}
        fields = [("cabin", "Cabin (optional)", extra.get("cabin_class", ""), [("All","")]+[(x,x) for x in ["Economy","Premium Economy","Business","First"]]),
                  ("family", "Fare family (optional)", extra.get("fare_family", ""), None),
                  ("scrape_status", "Scrape status (optional)", extra.get("scrape_status", ""), [("All","")]+[(x,x) for x in ["SUCCESS","PARTIAL"]]),
                  ("recipe", "Recipe version (optional)", extra.get("recipe_version", ""), None),
                  ("quality", "Quality flag (optional)", extra.get("quality_flags", ""), None)] + [("columns", "Selected columns (comma separated)", ",".join(spec["columns"]), None),
                  ("sort", "Sort by", spec["sort"], None), ("group", "Group by (optional)", ",".join(spec.get("group_by", [])), None),
                  ("aggregate", "Calculation, e.g. avg:total_fare (optional)", ",".join(x["function"] + ":" + x["column"] for x in spec.get("aggregates", [])), None)]
        async def submit(v):
            query = {**spec, "columns": [x.strip() for x in v["columns"].split(",") if x.strip()], "sort": v["sort"],
                     "group_by": [x.strip() for x in v["group"].split(",") if x.strip()], "offset": 0, "aggregates": []}
            for expr in v["aggregate"].split(","):
                if expr.strip():
                    function, column = expr.strip().split(":")
                    query["aggregates"].append({"function": function, "column": column})
            advanced = []
            for key, column, op in [("recipe", "recipe_version", "eq"), ("quality", "quality_flags", "contains"),("cabin","cabin_class","eq"),("family","fare_family","eq"),("scrape_status","scrape_status","eq")]:
                if v[key].strip():
                    advanced.append({"column": column, "op": op, "value": v[key].strip()})
            query["filters"] = [x for x in spec["filters"] if x["column"] not in {"recipe_version", "quality_flags","cabin_class","fare_family","scrape_status"}] + advanced
            await self.request("POST", "/api/v1/data/query", query)
            self.advanced_filters = advanced
            self.query_spec = query
        await self.push_screen(Form("Advanced Filters", fields, submit, save_label="Show Data"))


    async def action_page_info(self):
        messages = ["See if Udaan is working and what it has done recently.",
            "All Sources includes enabled READY recipes. All Windows creates T+1/7/15/30/45 jobs.\n\nNormal — Up to 3 website attempts; backs off if blocked.\nCareful — Up to 4 website attempts and asks you for help when needed.\nStrong — Up to 6 website attempts for unstable websites.\n\nParallel Scrapes limits open browser slots to 1–5. Only the challenged child pauses.",
            "Add websites and check if Udaan knows how to read them.",
            "View and export the data Udaan has collected.",
            "Eura Engine runs saved recipes independently of AI. Configure a model in Settings → AI to build or repair recipes. AI Memory supplies useful past fixes. Test AI makes an explicit provider request.",
            "See what Udaan is doing and why something failed.",
            "Change basic Udaan settings."]
        await self.push_screen(PageInfo(PAGES[self.page_index], messages[self.page_index]))

    def on_resize(self, event):
        for index, name in enumerate(PAGES):
            for button in self.query(f"#nav-{index}"):
                button.label = f"{index + 1} {name}" if event.size.width >= 100 else name
        for grid in self.query(".scrape-fields"):
            grid.set_class(event.size.width <= 80, "narrow")
