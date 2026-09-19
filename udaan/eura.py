"""Versioned declarative recipes; trusted execution and validation remain in Eura."""
import asyncio
import hashlib
import re
from decimal import Decimal
from typing import Literal
from urllib.parse import urlsplit

import yaml
from pydantic import Field, ValidationError, field_validator, model_validator
from sqlalchemy import select

from udaan.config import settings
from udaan.contracts import DomainError, Fare, Inspection, PageState, SearchRequest, Strict
from udaan.db import Recipe, Source, now
from udaan.planner_contracts import LearnedAction
from udaan.recipe_contract import DiscoveryContract, FamilyContract, FlightDetailsContract

EXTRACTION_FIELDS = {"cards", "total_fare", "currency", "airline", "flight_number", "fare_class", "base_fare", "taxes", "fees", "departure_time", "arrival_time"}


def valid_css(value):
    from lxml.cssselect import CSSSelector
    if not isinstance(value, str) or not 1 <= len(value) <= 500:
        raise ValueError("CSS selector must have 1 to 500 characters")
    CSSSelector(value)
    return value


class RecipePatch(Strict):
    selectors: dict[str, str] = Field(min_length=1, max_length=11)
    stability_ms: int = Field(1500, ge=500, le=5000)

    @field_validator("selectors")
    @classmethod
    def extraction_only(cls, value):
        if set(value) - EXTRACTION_FIELDS:
            raise ValueError("Only extraction selectors may be repaired")
        for selector in value.values():
            valid_css(selector)
        return value


class Step(Strict):
    action: Literal["click", "fill", "select", "airport", "date", "passengers", "cabin"]
    selector: str
    field: Literal["origin", "destination", "departure_date", "adults", "children", "infants", "cabin"] | None = None
    text: str | None = Field(None, max_length=80)
    optional: bool = False
    index: int = Field(0, ge=0, le=20, description="Zero-based position among visible elements matching THIS selector; usually 0. This is NOT the step number. For two airport inputs sharing a selector, use 0 for origin and 1 for destination.")
    @field_validator("selector")
    @classmethod
    def selector_valid(cls, value):
        return valid_css(value)


class Manifest(Strict):
    generated: bool = False
    entry_url: str | None = None
    navigation: Literal["steps", "indigo_booking", "akasa_booking", "aix_booking", "spicejet_booking"] = "steps"
    next_month_selector: str | None = None
    contract: Literal[1, 2, 3] = 1
    discovery: DiscoveryContract | None = None
    families: FamilyContract | None = None
    flight_details: FlightDetailsContract | None = None
    source_slug: str
    version: str = Field(pattern=r"^[a-zA-Z0-9._-]{1,80}$")
    allowed_hosts: list[str] = Field(min_length=1, max_length=10)
    search_ready: str
    result_ready: str
    empty_results: str
    context_selector: str
    context_date_selector: str | None = None
    context_date_attribute: Literal["id", "datetime", "data-date"] = "id"
    cabin_scopes: dict[str, str] = Field(default_factory=dict)
    route_selectors: dict[str, str] = Field(default_factory=dict)
    carrier_names: dict[str, str] = Field(default_factory=dict)
    single_adult_only: bool = False
    flight_number_many: bool = False
    search_steps: list[Step] = Field(default_factory=list, max_length=30)
    planner_steps: list[LearnedAction] = Field(default_factory=list, max_length=60)
    validated_search: dict[str, str] = Field(default_factory=dict)
    selectors: dict[str, str]
    field_attributes: dict[str, Literal["alt"]] = Field(default_factory=dict)
    stability_ms: int = Field(1500, ge=500, le=5000)

    @model_validator(mode="after")
    def valid(self):
        if self.entry_url:
            entry = urlsplit(self.entry_url)
            if self.generated or entry.scheme != 'https' or entry.hostname not in self.allowed_hosts or entry.username or entry.password or entry.query or entry.fragment:
                raise ValueError('Predefined entry URL must be an approved HTTPS booking page without credentials or query')
        if self.next_month_selector:
            valid_css(self.next_month_selector)
        if self.navigation != "steps" and (self.contract != 3 or self.generated or self.source_slug != {"indigo_booking":"indigo", "akasa_booking":"akasa-air", "aix_booking":"air-india-express", "spicejet_booking":"spicejet"}.get(self.navigation)):
            raise ValueError("Inspected navigation belongs to the predefined source contract")
        if self.contract == 3 and (self.generated or not self.discovery):
            raise ValueError("Common predefined recipes require inspected discovery controls")
        if self.contract == 1 and not self.search_steps:
            raise ValueError("Version 1 requires search steps")
        if self.contract == 2:
            from udaan.planner_contracts import NextAction
            if not self.generated or not self.planner_steps or self.search_steps:
                raise ValueError("Version 2 requires a verified planner sequence")
            if set(self.validated_search) != {"origin","destination","departure_date","adults","children","infants","cabin"}:
                raise ValueError("Planner recipes require the exact validated search scope")
            for step in self.planner_steps:
                valid_css(step.selector)
                NextAction(status="ACTION",action=step.action,target_id="e1",value=step.value)
                if not step.verified:
                    raise ValueError("Unverified actions cannot be compiled")
        for css in [self.search_ready, self.result_ready, self.empty_results, self.context_selector,
                    *([self.context_date_selector] if self.context_date_selector else []),
                    *self.cabin_scopes.values(), *self.route_selectors.values(), *self.selectors.values()]:
            valid_css(css)
        if set(self.selectors) - EXTRACTION_FIELDS or not {"cards", "total_fare", "currency", "airline"} <= set(self.selectors):
            raise ValueError("Extraction requires cards, total_fare, currency, and airline selectors")
        if set(self.cabin_scopes) - {"Economy", "Premium Economy", "Business", "First"}:
            raise ValueError("Unknown cabin scope")
        if self.route_selectors and (not {"origin", "destination"} <= set(self.route_selectors) or
                set(self.route_selectors) - {"origin", "destination", "alternate_origin", "alternate_destination"}):
            raise ValueError("Route selectors require origin and destination")
        if any(not re.fullmatch(r"[A-Z0-9]{2}", code) or not 1 <= len(name) <= 120 for code, name in self.carrier_names.items()):
            raise ValueError("Carrier names require explicit two-character flight prefixes")
        if set(self.field_attributes)-{"airline"}:
            raise ValueError("Only a displayed airline logo may supply alt text")
        for host in self.allowed_hosts:
            if not re.fullmatch(r"[a-z0-9.-]+", host) or "." not in host:
                raise ValueError("Use explicit public source hostnames")
        return self


def recipe_file(relative: str):
    root = settings().recipe_directory.resolve()
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root) or candidate.suffix not in {".yaml", ".yml"} or not candidate.is_file():
        raise DomainError("RECIPE_FILE_MISSING", "Recipe file is missing or outside the allowed recipe directory.")
    if candidate.stat().st_size > 100000:
        raise DomainError("RECIPE_INVALID", "Recipe manifest exceeds size limit")
    return candidate


def validate_manifest(manifest: Manifest, source: Source):
    host = urlsplit(source.base_url).hostname
    if manifest.source_slug != source.slug or host not in manifest.allowed_hosts:
        raise DomainError("RECIPE_INVALID", "Recipe belongs to another website/source identity.")
    if manifest.discovery and manifest.discovery.catalog_url:
        catalog = urlsplit(manifest.discovery.catalog_url)
        root = host.removeprefix("www.")
        if catalog.scheme != "https" or catalog.username or catalog.password or catalog.query or catalog.fragment or not (catalog.hostname == root or catalog.hostname.endswith("." + root)):
            raise DomainError("RECIPE_INVALID", "Airport catalog must belong to the public source domain")
    # Source attachments cannot grant navigation to unrelated domains.
    root = host.removeprefix("www.")
    approved = set(source.configuration.get("booking_hosts", []))
    if any(h != root and not h.endswith("." + root) and h not in approved for h in manifest.allowed_hosts):
        raise DomainError("RECIPE_INVALID", "Recipe hosts must belong to the configured source domain")


def register_recipe(db, source: Source, relative: str):
    path = recipe_file(relative)
    raw = path.read_bytes()
    try:
        manifest = Manifest.model_validate(yaml.safe_load(raw))
    except Exception as exc:
        raise DomainError("RECIPE_INVALID", "Recipe manifest failed contract validation", 422) from exc
    validate_manifest(manifest, source)
    digest = hashlib.sha256(raw).hexdigest()
    existing = db.scalar(select(Recipe).where(Recipe.source_id == source.id, Recipe.version == manifest.version))
    if existing:
        if existing.checksum != digest:
            raise DomainError("RECIPE_IMMUTABLE", "This version already exists with a different checksum")
        return existing
    recipe = Recipe(source_id=source.id, version=manifest.version, path=relative, checksum=digest,
                    manifest=manifest.model_dump(), health="VALIDATED", validated_at=now())
    db.add(recipe)
    db.flush()
    return recipe


def load_recipe(recipe: Recipe, source: Source):
    path = recipe_file(recipe.path)
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != recipe.checksum:
        raise DomainError("RECIPE_INVALID", "Recipe asset changed after registration; register a new version")
    try:
        manifest = Manifest.model_validate(yaml.safe_load(raw))
    except Exception as exc:
        raise DomainError("RECIPE_INVALID", "Recipe contract is invalid") from exc
    validate_manifest(manifest, source)
    return Eura(manifest, manifest.entry_url or source.base_url)


def attachment_problem(recipe, source):
    """Same validation contract for listing and attachment; never mutate on failure."""
    if recipe.source_id != source.id:
        return "RECIPE_WRONG_SOURCE", "Recipe belongs to another website/source."
    if not recipe.manifest or recipe.manifest.get("contract", 1) not in {1, 2, 3}:
        return "RECIPE_UNSUPPORTED_FORMAT", "Recipe uses an unsupported recipe format."
    if not recipe.validated_at:
        return "RECIPE_NOT_VALIDATED", "Recipe has not passed contract validation."
    try:
        load_recipe(recipe, source)
    except DomainError as exc:
        return exc.code, exc.message
    if recipe.manifest.get('contract') == 3 and recipe.successes < 2:
        return 'RECIPE_NOT_LIVE_VALIDATED', 'Recipe needs two successful real collection tests before activation.'
    if not recipe.live_validated_at:
        return "RECIPE_NOT_LIVE_VALIDATED", "Recipe has not passed a real browser validation."
    if recipe.health != "HEALTHY":
        return "RECIPE_NOT_USABLE", "Recipe needs a successful test before it can be selected."
    return None


def money(text, allow_zero=False):
    text = text.strip()
    matches = re.findall(r"(?<![\d.])\d[\d,]*(?:\.\d{1,2})?(?![\d.])", text)
    if len(matches) != 1 or re.search(r"[-−]\s*\d", text) or (text.startswith("(") and text.endswith(")")):
        raise DomainError("EXTRACTION_SCHEMA_FAILED", "Fare amount is ambiguous or invalid")
    token = matches[0]
    integer = token.split(".")[0]
    if "," in integer and not (re.fullmatch(r"\d{1,3}(?:,\d{3})+", integer) or
                               re.fullmatch(r"\d{1,2}(?:,\d{2})*,\d{3}", integer)):
        raise DomainError("EXTRACTION_SCHEMA_FAILED", "Fare amount has invalid digit grouping")
    value = Decimal(token.replace(",", ""))
    if value < 0 or (value == 0 and not allow_zero):
        raise DomainError("EXTRACTION_SCHEMA_FAILED", "Fare is not positive")
    return value


class Eura:
    def __init__(self, manifest: Manifest, base_url: str):
        self.manifest, self.base_url = manifest, base_url

    async def inspect(self, browser, request: SearchRequest, checkpoint="search"):
        barrier = await browser.security()
        if barrier:
            return barrier
        if urlsplit(browser.page.url).hostname not in self.manifest.allowed_hosts:
            return Inspection(state=PageState.INVALID, reason="Browser is outside the expected source")
        results = await browser.visible(self.manifest.result_ready)
        empty = await browser.visible(self.manifest.empty_results)
        if results is not None or empty is not None:
            if await self.context_matches(browser, request):
                return Inspection(state=PageState.READY, reason="Requested search results verified", checkpoint="results")
            return Inspection(state=PageState.INVALID, reason="Result route, date, cabin, or passenger count does not match the job")
        if await browser.visible(self.manifest.search_ready) is not None:
            return Inspection(state=PageState.READY, reason="Search form is available; parameters will be restored", checkpoint="search")
        return Inspection(state=PageState.INVALID, reason="Expected search form or verified result page is unavailable")

    async def context_matches(self, browser, request):
        if self.manifest.navigation == "spicejet_booking":
            from udaan.spicejet import context_matches
            return await context_matches(self, browser, request)
        if self.manifest.navigation == "aix_booking":
            from udaan.air_india_express import context_matches
            return await context_matches(self, browser, request)
        if self.manifest.navigation == "akasa_booking":
            from udaan.akasa import context_matches
            return await context_matches(self, browser, request)
        if self.manifest.navigation == "indigo_booking":
            from udaan.indigo import context_matches
            return await context_matches(self, browser, request)
        context = await browser.visible(self.manifest.context_selector)
        if context is None:
            return False
        text = (await context.inner_text()).casefold()
        day = request.departure_date
        date_tokens = [day.isoformat(), day.strftime("%d %b %Y").lower(), day.strftime("%d %B %Y").lower(),
                       day.strftime("%d/%m/%Y"), day.strftime("%a, %d %b %Y").lower()]
        date_ok = any(token in text for token in date_tokens)
        if self.manifest.context_date_selector:
            selected = await browser.visible(self.manifest.context_date_selector)
            stamp = await selected.get_attribute(self.manifest.context_date_attribute) if selected else None
            date_ok = (bool(stamp) and re.findall(r"\d{4}-\d{2}-\d{2}", stamp) == [day.isoformat()]
                       and day.strftime("%d %b").casefold() in text)
        route_ok = bool(re.search(rf"\b{request.origin.lower()}\b[\s\S]*?\b{request.destination.lower()}\b", text))
        cabin_mentions = set(re.findall(r"\b(?:premium economy|economy|business|first)\b", text))
        cabin_ok = cabin_mentions == {request.cabin.casefold()}
        passengers = {"adults": 0, "children": 0, "infants": 0}
        for before, before_kind, after_kind, after in re.findall(
                r"\b(?:(\d+)\s*(adults?|children|child|infants?)|(adults?|children|child|infants?)\s*(\d+))\b", text):
            count, kind = (before, before_kind) if before else (after, after_kind)
            key = "adults" if kind.startswith("adult") else "children" if kind.startswith("child") else "infants"
            passengers[key] += int(count)
        passengers_ok = passengers == {"adults": request.adults, "children": request.children, "infants": request.infants}
        return route_ok and date_ok and cabin_ok and passengers_ok

    async def collect(self, request, browser, guard, checkpoint):
        self._guard = guard
        self._checkpoint = checkpoint
        if self.manifest.contract == 3 and not self.manifest.families:
            raise DomainError("RECIPE_NOT_READY", "Airport discovery is implemented; complete fare-family extraction still requires validation")
        if self.manifest.single_adult_only and (request.adults, request.children, request.infants) != (1, 0, 0):
            raise DomainError("UNSUPPORTED_PASSENGERS", "This recipe currently supports one adult; other passenger combinations need validation")
        if browser.page.url == "about:blank":
            for attempt in range(browser.profile["transient_retries"] + 1):
                try:
                    await browser.navigate(self.base_url)
                    break
                except Exception as exc:
                    await guard()
                    if attempt == browser.profile["transient_retries"]:
                        raise DomainError("NAVIGATION_TIMEOUT", "Source navigation did not complete within the configured budget") from exc
                    await asyncio.sleep(self.manifest.stability_ms / 1000)
        await guard()
        if self.manifest.navigation in {"indigo_booking", "akasa_booking", "aix_booking", "spicejet_booking"}:
            if self.manifest.navigation == "spicejet_booking":
                from udaan.spicejet import search
            elif self.manifest.navigation == "indigo_booking":
                from udaan.indigo import search
            elif self.manifest.navigation == "aix_booking":
                from udaan.air_india_express import search
            else:
                from udaan.akasa import search
            try:
                if not await self.context_matches(browser, request):
                    await search(self, browser, request, guard, checkpoint)
                await checkpoint("Reading fare options")
                return await self.extract(browser, request)
            except DomainError:
                await guard()
                raise
            except Exception as exc:
                from udaan.worker import ResumeCheckpoint
                if isinstance(exc, ResumeCheckpoint):
                    raise
                # An overlay appearing during a click must enter operator handling.
                # A timeout alone is never classified as a security challenge.
                await guard()
                raise DomainError("EXTRACTION_STRUCTURE_FAILED", "A booking control did not become usable; the saved search needs checking") from exc
        if self.manifest.contract == 2:
            for field, expected in self.manifest.validated_search.items():
                if str(getattr(request, field)) != expected:
                    raise DomainError("RECIPE_SEARCH_SCOPE", "This learned recipe is validated only for its recorded route, date, cabin and passengers. Build/test a recipe for this search.")
            if not await self.context_matches(browser, request):
                await checkpoint("search")
                for step in self.manifest.planner_steps:
                    await guard()
                    if await self.context_matches(browser, request) and await browser.visible(self.manifest.result_ready):
                        break
                    ok, reason = await browser.planner_execute(step, request, guard)
                    if not ok:
                        raise DomainError("EXTRACTION_STRUCTURE_FAILED", reason)
            await checkpoint("results")
            return await self.extract(browser, request)
        deadline = asyncio.get_running_loop().time() + browser.profile["navigation_ms"] / 1000
        # Some sites mount their booking form only after their ordinary cookie dialog closes.
        for initial in self.manifest.search_steps:
            if not initial.optional:
                break
            await guard()
            await self.perform(initial, browser, request, guard)
        state = await self.inspect(browser, request)
        while state.state == PageState.INVALID and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.5)
            await guard()
            state = await self.inspect(browser, request)
        if state.state == PageState.READY and state.checkpoint == "results":
            return await self.extract(browser, request)
        if state.state != PageState.READY:
            raise DomainError("EXTRACTION_STRUCTURE_FAILED", state.reason)
        await checkpoint("search")
        for step in self.manifest.search_steps:
            await guard()
            # A manual operator may already have completed the search while clearing a challenge.
            state = await self.inspect(browser, request)
            if state.state == PageState.READY and state.checkpoint == "results":
                break
            try:
                await self.perform(step, browser, request, guard)
            except DomainError:
                raise
            except Exception as exc:
                await guard()
                raise DomainError("EXTRACTION_STRUCTURE_FAILED", "Search control did not become usable") from exc
            await guard()
            await asyncio.sleep(self.manifest.stability_ms / 1000)
            await guard()
        await checkpoint("results")
        deadline = asyncio.get_running_loop().time() + browser.profile["navigation_ms"] / 1000
        while True:
            before = asyncio.get_running_loop().time()
            waited = await guard()
            if waited:
                deadline += asyncio.get_running_loop().time() - before
            state = await self.inspect(browser, request, "results")
            if state.state == PageState.READY and state.checkpoint == "results":
                return await self.extract(browser, request)
            if asyncio.get_running_loop().time() > deadline:
                raise DomainError("EXTRACTION_STRUCTURE_FAILED", state.reason)
            await asyncio.sleep(self.manifest.stability_ms / 1000)

    async def perform(self, step, browser, request, guard):
        candidates = browser.page.locator(step.selector)
        if step.text and step.action == "click":
            candidates = candidates.filter(has_text=re.compile("^" + re.escape(step.text) + "$"))
        # Consent controls may arrive after the booking widget. Give optional
        # controls a bounded visibility wait before interacting with the form.
        deadline = asyncio.get_running_loop().time() + (8 if step.optional else 0)
        while True:
            visible = [candidates.nth(i) for i in range(min(await candidates.count(), 30)) if await candidates.nth(i).is_visible()]
            target = visible[step.index] if len(visible) > step.index else None
            if target is not None or asyncio.get_running_loop().time() >= deadline:
                break
            await guard()
            await asyncio.sleep(0.25)
        if target is None:
            if step.optional:
                return
            raise DomainError("EXTRACTION_STRUCTURE_FAILED", "Required search control is missing")
        value = str(getattr(request, step.field)) if step.field else step.text
        if self.manifest.generated:
            kind = await target.get_attribute("type")
            if kind in {"password", "hidden"}:
                raise DomainError("RECIPE_ACTION_REJECTED", "Recipe selected a restricted control")
            if step.action == "click":
                label = (await target.get_attribute("aria-label") or await target.inner_text()).strip()
                if not re.fullmatch(r"search(?: flights)?|find flights|one way|round trip|accept all|done|apply", label, re.I):
                    raise DomainError("RECIPE_ACTION_REJECTED", "Recipe click is not an allowed flight-search control")
        if step.action == "click":
            await target.click()
        elif step.action == "fill":
            await target.fill(value)
        elif step.action == "select":
            await target.select_option(label=value)
        elif step.action == "airport":
            await target.fill(value)
            await asyncio.sleep(self.manifest.stability_ms / 1000)
            await guard()
            option = browser.page.get_by_role("option").filter(has_text=re.compile(rf"\b{re.escape(value)}\b"))
            if await option.count() == 1:
                await option.click()
            else:
                match = browser.page.get_by_text(re.compile(rf"\({re.escape(value)}\)"))
                if await match.count() == 1:
                    await match.click()
                else:
                    raise DomainError("EXTRACTION_STRUCTURE_FAILED", "Airport suggestion was not unambiguous")
        elif step.action == "date":
            if await target.get_attribute("type") == "date":
                await target.fill(value)
            else:
                calendar = browser.page.get_by_role("button", name="Next month", exact=True).first
                if not await calendar.is_visible():
                    await target.click()
                day = request.departure_date
                labels = [day.strftime("%d %B %Y"), day.isoformat(),
                          day.strftime("%B %-d, %Y"), day.strftime("%-m/%-d/%Y")]
                await browser.page.get_by_role("button", name="Next month", exact=True).first.wait_for(state="visible")
                for _ in range(13):
                    await guard()
                    chosen = None
                    for label in labels:
                        # Exact names avoid the browser driver's broken slash serialization in date regexes.
                        matches = browser.page.get_by_role("button", name=label, exact=True)
                        visible = [matches.nth(i) for i in range(await matches.count()) if await matches.nth(i).is_visible()]
                        if len(visible) == 1:
                            chosen = visible[0]
                            break
                    if chosen is not None:
                        await chosen.click()
                        break
                    next_month = browser.page.get_by_role("button", name="Next month", exact=True).first
                    await next_month.click()
                else:
                    raise DomainError("EXTRACTION_STRUCTURE_FAILED", "Requested calendar date is unavailable")
        elif step.action == "cabin":
            if await target.evaluate("e => e.tagName") == "SELECT":
                await target.select_option(label=request.cabin)
            else:
                await target.click()
                await guard()
                await browser.page.get_by_text(request.cabin, exact=True).last.click()
        elif step.action == "passengers":
            if await target.get_attribute("aria-expanded") != "true":
                await target.click()
            await browser.page.locator(".ai-pax-selector__passenger-row").first.wait_for(state="visible")
            for label, desired in [("Adults", request.adults), ("Children", request.children), ("Infants", request.infants)]:
                await guard()
                field = browser.page.get_by_role("spinbutton", name=re.compile(label, re.I))
                if await field.count() == 1:
                    await field.fill(str(desired))
                else:
                    row = browser.page.locator(".ai-pax-selector__passenger-row").filter(has_text=re.compile("^" + label))
                    if await row.count() != 1:
                        raise DomainError("EXTRACTION_STRUCTURE_FAILED", "Passenger selector requires an updated recipe")
                    current = int(await row.locator(".ai-pax-selector__counter-value").inner_text())
                    for _ in range(abs(desired - current)):
                        await guard()
                        direction = "Increase" if desired > current else "Decrease"
                        await row.get_by_role("button", name=f"{direction} {label}", exact=True).click()
                    if int(await row.locator(".ai-pax-selector__counter-value").inner_text()) != desired:
                        raise DomainError("EXTRACTION_STRUCTURE_FAILED", "Passenger quantity could not be verified")
            done = browser.page.get_by_role("button", name=re.compile(r"^(done|apply)$", re.I))
            if await done.count() == 1:
                await guard()
                await done.click()
            else:
                await browser.page.keyboard.press("Escape")

    async def extract(self, browser, request):
        if await browser.security() or not await self.context_matches(browser, request):
            raise DomainError("EXTRACTION_SCHEMA_FAILED", "Result context changed before extraction")
        if await browser.visible(self.manifest.empty_results) is not None:
            return []
        if self.manifest.navigation == "spicejet_booking":
            from udaan.spicejet import extract
            return await extract(self, browser, request)
        if self.manifest.contract == 3:
            from udaan.family_extraction import extract_families
            return await extract_families(self, browser, request)
        from scrapling.parser import Selector
        document = Selector(await browser.page.content())
        cards = document.css(self.manifest.selectors["cards"])
        if not cards:
            raise DomainError("EXTRACTION_STRUCTURE_FAILED", "Verified result page contains no extractable fare cards")
        fares = []
        for card in cards:
            if self.manifest.route_selectors:
                route = {}
                for name, css in self.manifest.route_selectors.items():
                    nodes = card.css(css)
                    route[name] = nodes[0].get_all_text().strip() if nodes else None
                actual_origin = route.get("alternate_origin") or route.get("origin")
                actual_destination = route.get("alternate_destination") or route.get("destination")
                if (actual_origin, actual_destination) != (request.origin, request.destination):
                    continue  # Do not mislabel a nearby airport as the requested airport.
            fare_scope = card
            if self.manifest.cabin_scopes:
                css = self.manifest.cabin_scopes.get(request.cabin)
                scopes = card.css(css) if css else []
                if not scopes:
                    continue  # This flight does not offer the requested cabin.
                if len(scopes) != 1:
                    raise DomainError("EXTRACTION_SCHEMA_FAILED", "Requested cabin price is ambiguous")
                fare_scope = scopes[0]
            def read(key):
                css = self.manifest.selectors.get(key)
                root = fare_scope if key in {"total_fare", "currency", "base_fare", "taxes", "fees", "fare_class"} else card
                nodes = root.css(css) if css else []
                if len(nodes) > 1 and key in {"total_fare", "currency"}:
                    raise DomainError("EXTRACTION_SCHEMA_FAILED", "Fare card contains ambiguous prices or currencies")
                if nodes and key in self.manifest.field_attributes:
                    return nodes[0].attrib.get(self.manifest.field_attributes[key])
                return nodes[0].get_all_text().strip() if nodes else None
            total, currency = read("total_fare"), read("currency")
            if not total or not currency:
                raise DomainError("EXTRACTION_SCHEMA_FAILED", "A fare card is missing its total or explicit currency")
            currency_match = re.search(r"\b(INR|USD|EUR|GBP|AED|SGD|AUD|CAD)\b", currency.upper())
            if not currency_match:
                if "₹" in currency:
                    currency_code = "INR"
                else:
                    raise DomainError("EXTRACTION_SCHEMA_FAILED", "Currency is not unambiguous")
            else:
                currency_code = currency_match.group()
            airline = read("airline")
            carrier_basis = "displayed_operator"
            if airline:
                airline = re.sub(r"^Operated by\s+", "", airline, flags=re.I)
            elif self.manifest.carrier_names:
                prefix = re.match(r"^([A-Z0-9]{2})\s*\d", read("flight_number") or "")
                airline = self.manifest.carrier_names.get(prefix.group(1)) if prefix else None
                carrier_basis = "displayed_marketing_flight_prefix"
            data = {"origin": request.origin, "destination": request.destination, "departure_date": request.departure_date,
                    "cabin": request.cabin, "booking_window": request.booking_window,
                    "airline": airline, "total_fare": money(total),
                    "currency": currency_code, "flight_number": read("flight_number"), "fare_class": read("fare_class"),
                    "extraction_metadata": {"selector_version": self.manifest.version, "fare_basis": "displayed_from_price_per_adult" if self.manifest.single_adult_only else "displayed_search_total",
                                            "carrier_basis": carrier_basis,
                                            "passengers": {"adults": request.adults, "children": request.children, "infants": request.infants}}}
            for time_field in ["departure_time", "arrival_time"]:
                displayed = read(time_field)
                if displayed:
                    match = re.fullmatch(r"(?:[01]?[0-9]|2[0-3]):[0-5][0-9](?:\s*[AP]M)?", displayed, re.I)
                    if not match:
                        raise DomainError("EXTRACTION_SCHEMA_FAILED", "Displayed flight time could not be validated")
                    # No timezone is invented; UTC timestamps remain nullable.
                    data["extraction_metadata"][time_field + "_local"] = displayed
            for component in ["base_fare", "taxes", "fees"]:
                value = read(component)
                data[component] = money(value, allow_zero=True) if value else None
            try:
                fares.append(Fare.model_validate(data))
            except ValidationError as exc:
                raise DomainError("EXTRACTION_SCHEMA_FAILED", "Extracted fare failed the canonical observation contract") from exc
        if not fares:
            raise DomainError("EXTRACTION_SCHEMA_FAILED", "No displayed fares matched the exact requested airports and cabin")
        return fares
