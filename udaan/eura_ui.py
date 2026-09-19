"""Product screens for Eura, using the existing API and console style."""
from datetime import date, timedelta

from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Collapsible, Input, Label, Static

from udaan.tui import Form, JobDetails, short_time

AI_HELP = "Add an AI provider in Settings to build recipes automatically."


def model_status(value):
    if value in {"UNCONFIGURED", "NOT CONFIGURED"}:
        return "NOT CONFIGURED"
    if value in {"READY", "CHECKING", "NOT TESTED"}:
        return value
    return "FAILED"


def model_label(value):
    if not value:
        return "Not configured"
    name = value.rsplit("/", 1)[-1]
    words = [word for word in name.split("-") if not __import__("re").fullmatch(r"(?:\d+b|a\d+b)", word, __import__("re").I)]
    return " ".join(word.capitalize() if not __import__("re").fullmatch(r"\d+(?:\.\d+)?", word) else word
                    for word in words)


class ModelSettings(ModalScreen):
    BINDINGS = [("escape", "dismiss", "Close")]

    def __init__(self, config):
        super().__init__()
        self.config = config

    def compose(self):
        c = self.config
        with Vertical(classes="dialog form-dialog"):
            yield Label("Settings → AI", classes="dialog-title")
            yield Static("OpenAI-compatible provider · " + model_status(c.get("ai_status")), id="model-status")
            with VerticalScroll(classes="form-fields"):
                for name, value in [("Provider Name", c.get("ai_provider_name")), ("Base URL", c.get("ai_base_url")), ("Model", c.get("ai_model"))]:
                    with Horizontal(classes="eura-field"):
                        yield Label(name)
                        yield Input(value or "", disabled=True)
                with Horizontal(classes="eura-field"):
                    yield Label("API Key")
                    yield Input("", password=True, disabled=True, placeholder="Configured (hidden)" if c.get("ai_key_configured") else "Not configured", id="model-key")
                yield Static("\nConfiguration: .env / process environment\n"
                             "Set AI_PROVIDER=openai_compatible, AI_PROVIDER_NAME, AI_BASE_URL, AI_MODEL and AI_API_KEY.\n"
                             "Restart the API and worker after changes. Keep the key in your local environment; it is never displayed here.\n"
                             "Test AI contacts the configured provider only when requested.\n", markup=False)
            with Horizontal(classes="buttons"):
                yield Button("Test AI", id="model-test", disabled=not c.get("ai_configured"))
                yield Button("Close", id="model-close")

    def on_mount(self):
        self.set_interval(2, self.refresh_status)

    async def refresh_status(self):
        try:
            health = await self.app.request("GET", "/api/v1/health")
            model = health["model"]
            self.query_one("#model-status", Static).update("OpenAI-compatible provider · " + model_status(model["connectivity"]) + "\n" + (model.get("last_error") or ""))
        except Exception:
            self.query_one("#model-status", Static).update("Status unavailable — check API connection.")

    async def on_button_pressed(self, event):
        event.stop()
        if event.button.id == "model-close":
            self.dismiss()
        else:
            try:
                await self.app.request("POST", "/api/v1/health/refresh")
                await self.refresh_status()
            except Exception as exc:
                self.query_one("#model-status", Static).update(str(exc))


class RecipeBuild(Form):
    def __init__(self, source, health):
        self.source, self.health = source, health
        configured = health["model"].get("configured", False)
        self.available = configured and health["services"].get("eura", {}).get("status") == "READY"
        super().__init__("Build Recipe — Eura AI", [
            ("origin", "Test route — From", "DEL", None), ("destination", "To", "BOM", None),
            ("date", "Travel Date (one adult, Economy)", (date.today() + timedelta(days=30)).isoformat(), None)], self.start, save_label="START")

    def compose(self):
        with Vertical(classes="dialog form-dialog"):
            yield Label("BUILD RECIPE", classes="dialog-title")
            yield Static(f"Website     {self.source['name']}\nEngine      Eura\nAI          {model_status(self.health['model']['connectivity'])}\n", markup=False)
            yield Static("○ Open website\n○ Read page\n○ Ask AI\n○ Test recipe\n○ Save recipe\n", id="build-steps")
            with VerticalScroll(classes="form-fields"):
                for key, label, value, _ in self.fields:
                    with Horizontal(classes="eura-field"):
                        yield Label(label)
                        yield Input(value, id="f-" + key)
            explanation = "" if self.available else "AI provider is not configured.\n" + AI_HELP if not self.health['model'].get('configured') else "Eura Engine is unavailable. Check Advanced."
            yield Static(explanation, id="form-error", markup=False)
            with Horizontal(classes="buttons"):
                yield Button("START", id="save", variant="primary", disabled=not self.available)
                yield Button("Close", id="close")

    async def start(self, values):
        search = {"origin": values["origin"].strip().upper(), "destination": values["destination"].strip().upper(),
                  "departure_date": values["date"], "booking_window": (date.fromisoformat(values["date"]) - date.today()).days,
                  "adults": 1, "cabin": "Economy"}
        result = await self.app.request("POST", f"/api/v1/sources/{self.source['id']}/build-recipe", search)
        self.app.current_job = result["id"]
        self.app.call_after_refresh(lambda: self.app.push_screen(JobDetails(result["id"])))


class RecipeDetails(ModalScreen):
    BINDINGS = [("escape", "dismiss", "Close")]

    def __init__(self, source):
        super().__init__()
        self.source, self.summary, self.health = source, {}, {}

    def compose(self):
        with VerticalScroll(classes="dialog"):
            yield Label("Recipe — " + self.source["name"], classes="dialog-title")
            yield Static("Checking recipe…", id="recipe-summary", markup=False)
            yield Static("", id="recipe-help", markup=False)
            with Horizontal(classes="buttons"):
                yield Button("Test Recipe", id="recipe-test", disabled=True)
                yield Button("Choose Version", id="recipe-choose")
                yield Button("Close", id="recipe-close")
            with Horizontal(classes="buttons"):
                yield Button("Build with Eura AI", id="recipe-build", disabled=True)
                yield Button("Repair with Eura AI", id="recipe-repair", disabled=True)
            with Collapsible(title="Advanced", collapsed=True):
                yield Static("", id="recipe-advanced", markup=False)

    async def on_mount(self):
        await self.refresh_recipe()
        self.set_interval(3, self.refresh_recipe)

    async def refresh_recipe(self):
        try:
            self.summary = s = await self.app.request("GET", f"/api/v1/sources/{self.source['id']}/recipe-status")
            self.health = await self.app.request("GET", "/api/v1/health")
            engine = self.health['services'].get('eura', {})
            configured = self.health['model'].get('configured', False)
            self.query_one("#recipe-summary", Static).update(
                f"Website          {self.source['name']}\nCurrent Recipe   {s.get('version') or 'None'}\n"
                f"Status           {s['status']}\nBuilt By         {s.get('built_by') or '—'}\n"
                f"Last Tested      {short_time(s.get('last_tested'))}\nLast Repair      {short_time(s.get('last_repair'))}\n"
                f"Eura             {engine.get('status', 'CHECKING')}\nAI               {model_status(self.health['model']['connectivity'])}\n")
            self.query_one("#recipe-help", Static).update(
                ("No working recipe.\n" if s['status'] == 'NO RECIPE' else '') + (AI_HELP if not configured else "Build and repair save a recipe only after validation."))
            self.query_one("#recipe-test", Button).disabled = not s.get('recipe_id')
            # Build remains accessible to explain the missing-provider state; START is disabled.
            self.query_one("#recipe-build", Button).disabled = False
            self.query_one("#recipe-repair", Button).disabled = not (configured and s['status'] == 'DEGRADED' and engine.get('status') == 'READY')
            from udaan.tui import pretty
            self.query_one("#recipe-advanced", Static).update(pretty({'engine': engine, 'recipe': s}))
        except Exception as exc:
            self.query_one("#recipe-help", Static).update(str(exc))

    async def on_button_pressed(self, event):
        event.stop()
        action = event.button.id
        if action == "recipe-close":
            self.dismiss()
        elif action == "recipe-build":
            await self.app.push_screen(RecipeBuild(self.source, self.health))
        elif action == "recipe-choose":
            await self.app.choose_recipe(self.source)
        elif action == "recipe-test":
            await self.app.test_recipe(self.source, self.summary['recipe_id'])
        elif action == "recipe-repair":
            try:
                repair = await self.app.request("POST", f"/api/v1/sources/{self.source['id']}/repair-recipe")
                await self.app.push_screen(RepairProgress(repair['id']))
            except Exception as exc:
                self.query_one("#recipe-help", Static).update(str(exc))


class RepairProgress(ModalScreen):
    BINDINGS = [("escape", "dismiss", "Close")]

    def __init__(self, identifier):
        super().__init__()
        self.identifier = identifier

    def compose(self):
        with Vertical(classes="dialog"):
            yield Label("Repair with Eura AI", classes="dialog-title")
            yield Static("Checking website…", id="repair-progress", markup=False)
            yield Button("Close", id="repair-close")

    async def on_mount(self):
        await self.refresh_progress()
        self.set_interval(2, self.refresh_progress)

    async def refresh_progress(self):
        try:
            result = await self.app.request('GET', '/api/v1/repairs/' + self.identifier)
            labels = {'PENDING': 'Checking website…', 'GENERATING': 'Reading page…',
                      'MEMORY': 'Looking at past fixes…', 'ASKING_AI': 'Asking AI…',
                      'GENERATED': 'Testing new recipe…', 'STATIC_VALIDATION': 'Testing new recipe…',
                      'ISOLATED_TEST': 'Testing new recipe…', 'CONTRACT_VALIDATION': 'Testing new recipe…',
                      'LIVE_VALIDATION': 'Testing new recipe on the website…', 'APPROVED': 'Saving recipe…',
                      'PROMOTED': 'New recipe ready.'}
            self.query_one('#repair-progress', Static).update(labels.get(result['state'], result.get('reason') or result['state']))
        except Exception as exc:
            self.query_one('#repair-progress', Static).update(str(exc))

    def on_button_pressed(self, event):
        event.stop()
        self.dismiss()
