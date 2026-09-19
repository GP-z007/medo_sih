"""Observe → typed choice → trusted browser action → verify; isolate compiled recipes before promotion."""

import asyncio
import re

from pydantic import ValidationError

from udaan.contracts import DomainError, Inspection, PageState
from udaan.planner_contracts import LearnedAction, NextAction

GOALS = ["origin", "destination", "departure_date", "adults", "cabin", "submit"]


def mapping_problem(field, node, target, request):
    """Reject structurally valid model choices that contradict the observed field."""
    text = (node.get('label') if node.get('tag') == 'img' else node.get('text')) or ''
    selector = target.get('selector', '').casefold()
    if field in {'total_fare', 'currency'}:
        for token, cabin in [('business-class','Business'), ('economy-class','Economy'), ('premium-economy','Premium Economy')]:
            if token in selector and request.cabin != cabin:
                return 'The selected amount belongs to ' + cabin + '; map the requested ' + request.cabin + ' cabin.'
    if field == 'total_fare':
        from udaan.eura import money
        try:
            money(text)
        except DomainError:
            return 'Choose one displayed monetary amount, not a container with multiple prices or times.'
    if field == 'airline' and (not text or len(text) > 120 or re.search(r'₹|\bINR\b|\d{1,2}:\d{2}', text)):
        return 'Choose the displayed airline name or airline logo, not the entire flight card.'
    return None


async def ask(provider, context, guard, record, allowed_actions=None):
    await guard()
    try:
        schema = NextAction.model_json_schema()
        if allowed_actions:
            schema['properties']['action'] = {'anyOf':[{'type':'string','enum':allowed_actions},{'type':'null'}]}
            schema['properties']['value'] = {'type':'null'}
        content = await provider.generate_recipe(context, schema, planner=True)
        try:
            response = NextAction.model_validate_json(content)
        except ValidationError as exc:
            # Never persist Pydantic's input or user-supplied error message.
            issues = exc.errors(include_input=False, include_url=False)
            location = '.'.join(str(x) for x in issues[0]['loc']) or 'action'
            raise DomainError("MODEL_SCHEMA_ERROR", f"AI action field {location} did not match the allowed format ({issues[0]['type']}).") from exc
        if response.status == 'ACTION' and allowed_actions and response.action not in allowed_actions:
            raise DomainError('MODEL_SCHEMA_ERROR', 'AI selected an action outside the current extraction step.')
        targets = {e["target_id"] for e in context["PAGE_STATE"]["elements"]}
        if response.status == "ACTION" and response.target_id not in targets:
            raise DomainError(
                "MODEL_SCHEMA_ERROR", "AI selected an element that was not in the current page state."
            )
        await guard()
        record(
            "MODEL_REQUEST", **{**getattr(provider, "last_request", {}), "response_validity": "SCHEMA_VALID"}
        )
        return response
    except DomainError as exc:
        metrics = {**getattr(provider, "last_request", {}), "response_validity": exc.code}
        record("MODEL_REQUEST", **metrics)
        raise


async def learn(browser, request, provider, memory, source_id, guard, flow, stage, record, persist, isolate):
    from udaan.eura import Manifest

    steps, goals = [], {}
    deferred = set()
    initial_selector = None

    async def choice(goal, state, previous=None, actions=None):
        stage("Looking at past fixes")
        knowledge = await memory.search(source_id, goal + " flight search")
        record("MEMORY_RETRIEVED", goal=goal, count=len(knowledge), retrieval="BM25")
        for correction in range(3):
            try:
                stage("Asking AI")
                return await ask(
                    provider,
                    {
                        "goal": goal,
                        "PAGE_STATE": state,
                        "previous_result": previous,
                        "parameters": {key: "{{" + key + "}}" for key in GOALS if key != "submit"},
                        "requested_search": request.model_dump(mode="json"),
                        "memory": knowledge,
                        "rule": (
                            "Choose exactly one supplied element. A successful input action is followed by deterministic verification. "
                            "For origin or destination, fill an input with its supplied parameter; click only an observed airport option."
                        ),
                    },
                    guard,
                    record,
                    allowed_actions=actions,
                )
            except DomainError as exc:
                if not actions or correction == 2 or exc.code not in {"MODEL_SCHEMA_ERROR","MODEL_INVALID_RESPONSE","MODEL_EMPTY_RESPONSE"}:
                    raise
                previous = {"result":exc.message,"correction":"Use the one permitted extraction action and a supplied target_id. value must be null; do not return extracted values."}


    # A known ordinary consent banner can cover every booking control. Dismiss
    # its explicit accept action deterministically before asking AI about goals.
    visible = getattr(browser, "visible", None)
    if visible:
        consent = await visible('#onetrust-accept-btn-handler')
        if consent is not None:
            await guard()
            await consent.click()
            await asyncio.sleep(.5)
            await guard()
            record("PLANNER_ACTION", goal="ordinary_consent", action="click", verified=True, reason="OneTrust banner dismissed")
    stage("Finding controls")
    for goal in GOALS:
        previous, expanded = None, False
        stage("Learning search steps", goal=goal)
        for attempt in range(10):
            await guard()
            state = await browser.page_state(request, goal, expanded=expanded)
            if not state:
                await guard()
                continue
            if not state["elements"]:
                raise DomainError(
                    "RECIPE_CONTEXT_INSUFFICIENT",
                    "No safe visible flight-search controls were found for this step.",
                )
            if goal == "cabin" and attempt == 0:
                state = await browser.page_state(request, goal, expanded=True)
                if not state:
                    await guard()
                    continue
                if not any(re.search(r"\b(cabin|class|economy|business|first)\b", " ".join(str(n.get(k,"")) for k in ["label","text","placeholder","name"]), re.I) for n in state["elements"]):
                    # Some sites choose cabin on results, not the search form.
                    # Absence is not proof of Economy; live result validation is still mandatory.
                    deferred.add(goal)
                    record("PLANNER_GOAL_DEFERRED", goal=goal, reason="No cabin control is exposed before search; cabin must be verified on results.")
                    break
            if initial_selector is None:
                # Wait for the client-rendered booking control, not marketing/navigation placeholders.
                deadline = asyncio.get_running_loop().time() + 30
                while not state or not any(re.search(r"\b(from|origin)\b", x.get("label", "")+' '+x.get("placeholder", ""), re.I) for x in state["elements"]):
                    if asyncio.get_running_loop().time() >= deadline:
                        raise DomainError("RECIPE_CONTEXT_INSUFFICIENT", "The website did not show an origin control within the booking-form wait.")
                    await asyncio.sleep(.5)
                    await guard()
                    state = await browser.page_state(request, goal, expanded=expanded)
                    if not state:
                        continue
                # Deterministically discovered root readiness locator, never model-authored.
                initial_selector = next(iter(browser._planner_targets.values()))["selector"]
            if attempt == 0:
                await memory.remember(
                    source_id,
                    "control",
                    goal,
                    {
                        "state": "DISCOVERED",
                        "fields": [
                            {k: e.get(k, "") for k in ["tag", "role", "label", "placeholder"]}
                            for e in state["elements"][:5]
                        ],
                    },
                )
                record("MEMORY_SAVED", goal=goal, memory_kind="control discovery")
            try:
                action = await choice(goal, state, previous)
            except DomainError as exc:
                if (
                    exc.code not in {"MODEL_SCHEMA_ERROR", "MODEL_INVALID_RESPONSE", "MODEL_EMPTY_RESPONSE"}
                    or attempt >= 2
                ):
                    raise
                previous = {
                    "result": exc.message,
                    "correction": "Choose one supplied target and match the action schema.",
                }
                continue
            if action.status == "MANUAL_ACTION_REQUIRED":
                await flow.wait(
                    Inspection(
                        state=PageState.MANUAL_ACTION_REQUIRED,
                        reason="The planner cannot continue this search step without manual operator help.",
                    )
                )
                previous = "Operator requested a re-check; inspect current state before any action."
                continue
            if action.status == "NEED_MORE_CONTEXT":
                if expanded:
                    raise DomainError(
                        "RECIPE_GENERATION_FAILED",
                        "AI could not identify the next search action from the available page context.",
                    )
                expanded = True
                previous = "More nearby flight-search controls are now included."
                continue
            target = browser._planner_targets[action.target_id]
            if action.action in {"fill", "select"} and action.value != "{{"+goal+"}}":
                previous = {"result":"The chosen input value does not match this goal."}
                continue
            if goal in {"origin", "destination"} and target["tag"] == "input":
                semantic = " ".join(str(target.get(key, "")) for key in ("label", "placeholder", "name"))
                expected = r"\b(?:from|origin)\b" if goal == "origin" else r"\b(?:to|destination)\b"
                if not re.search(expected, semantic, re.I):
                    previous = {"result": f"The selected input is not the {goal} field."}
                    continue
                if action.action == "click" and not target.get("readonly"):
                    # The model chose the observed editable field; normalize its open-click to a bounded fill.
                    action = action.model_copy(update={"action": "fill", "value": "{{"+goal+"}}"})
            if action.action == "click":
                label = target["label"] or target["text"] or target["placeholder"]
                if re.fullmatch(r"cookies?", label.strip(), re.I):
                    previous = {"result":"Cookies opens preference settings; choose an observed Accept All or Close control instead."}
                    continue
                if not re.search(r"from|\bto\b|origin|destination|airport|search|flight|one way|round trip|date|depart|arrival|return|calendar|month|next|previous|adult|child|infant|traveller|passenger|cabin|economy|business|first|accept|cookie|done|apply|close|\b[A-Z]{3}\b|\b\d{1,4}\b", label, re.I):
                    previous = {"result":"Choose a flight-search control for this goal; the last target was unrelated."}
                    continue
            learned = LearnedAction(
                action=action.action,
                selector=target["selector"],
                goal=goal,
                value=action.value,
                label=target["label"] or target["text"] or target["placeholder"],
                tag=target["tag"],
                calendar_day=bool(target.get("calendar_date")) and goal=="departure_date",
                verified=False,
            )
            persist(
                {
                    "phase": "SEARCH",
                    "steps": [x.model_dump() for x in steps],
                    "pending_action": learned.model_dump(),
                    "completed_goals": list(goals),
                }
            )
            # Exploration uses the trusted, bounded driver on a discovered target.
            # No generated code is executed. Isolation tests the compiled candidate below.
            stage("Checking action", goal=goal)
            node = next(x for x in state["elements"] if x["target_id"] == action.target_id)
            if not node.get("visible") or not node.get("enabled"):
                previous = {"result":"The target is not visible and enabled; choose a usable control."}
                continue
            NextAction.model_validate(action.model_dump())
            ok, reason = await browser.planner_execute(learned, request, guard)
            record(
                "PLANNER_ACTION",
                goal=goal,
                action=action.action,
                target_id=action.target_id,
                verified=ok,
                reason=reason,
            )
            if not ok:
                previous = {"failed_action": action.model_dump(exclude={"reason"}), "result": reason}
                await memory.remember(
                    source_id,
                    "failure",
                    goal,
                    {"action": action.action, "label": learned.label, "reason": reason},
                )
                continue
            learned.verified = True
            steps.append(learned)
            await memory.remember(
                source_id,
                "control",
                goal,
                {
                    "action": action.action,
                    "label": learned.label,
                    "tag": learned.tag,
                    "role": target["role"],
                    "verified": True,
                },
            )
            previous = "Action had a verified effect; the current goal still needs completion."
            # Action success and goal completion are different checks.
            if goal == "submit":
                candidates = await browser.result_candidates()
                deadline = asyncio.get_running_loop().time() + 30
                while not candidates and asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(.5)
                    await guard()
                    candidates = await browser.result_candidates()
                done = bool(candidates)
            else:
                after = await browser.page_state(request, goal, expanded=True)
                done = False
                if after:
                    for node in browser._planner_targets.values():
                        semantic = " ".join(
                            node.get(k, "") for k in (["label", "placeholder", "name"] if goal in {"origin","destination"} else ["label", "placeholder", "name", "nearby"])
                        ).lower()
                        terms = {
                            "origin": ["from", "origin"],
                            "destination": ["to", "destination"],
                            "departure_date": ["date", "depart"],
                            "adults": ["adult", "passenger", "traveller"],
                            "cabin": ["cabin", "class", "economy"],
                        }[goal]
                        if any(term in semantic for term in terms) and await browser.planner_value_matches(
                            node["selector"], request, goal
                        ):
                            # Open airport suggestions must be chosen before advancing.
                            if goal in {"origin", "destination"} and any(
                                (x.get("role") == "option" or (x.get("role") == "combobox" and x.get("tag") != "input" and not x.get("label") and re.search(r"\b[A-Z]{3}\b",x.get("text", "")))) for x in after["elements"]
                            ):
                                continue
                            goals[goal] = node["selector"]
                            done = True
                            break
            if done and goal == "adults" and after:
                pending = [n for n in after["elements"] if n.get("enabled") and re.search(r"\b(continue|done|apply)\b", n.get("label", "")+" "+n.get("text", ""), re.I)]
                if pending:
                    done = False
                    goals.pop(goal, None)
                    previous = "Passenger count matches. Finish the open passenger picker with its observed Continue, Done or Apply control."
            if done:
                if goal == "submit":
                    goals[goal] = "results"
                persist(
                    {
                        "phase": "SEARCH",
                        "steps": [x.model_dump() for x in steps],
                        "completed_goals": list(goals),
                    }
                )
                break
        else:
            raise DomainError(
                "RECIPE_GENERATION_FAILED",
                f"Could not verify the {goal.replace('_', ' ')} step within 10 actions.",
            )
    stage("Testing search")
    if set(goals) | deferred != set(GOALS):
        raise DomainError(
            "RECIPE_VALIDATION_FAILED", "Search identity and submission have not all been verified."
        )
    stage("Finding flight results")
    candidates = await browser.result_candidates()
    if not candidates:
        raise DomainError(
            "RECIPE_VALIDATION_EMPTY",
            "No repeated flight results were found; an empty search cannot validate a recipe.",
        )
    state = {"elements": candidates}
    selected = await choice("Choose the repeated container for one flight result; use extract_text.", state, actions=["extract_text"])
    if (
        selected.status != "ACTION"
        or selected.action != "extract_text"
        or selected.target_id not in browser._result_candidates
    ):
        raise DomainError("MODEL_SCHEMA_ERROR", "AI did not select an observed flight-result container.")
    cards = browser._result_candidates[selected.target_id]["selector"]
    await memory.remember(
        source_id,
        "results",
        "result cards",
        {"pattern": cards, "repeated": browser._result_candidates[selected.target_id]["repeated"]},
    )
    mapping = {"cards": cards}
    attributes = {}
    persist({"phase":"FIELDS","steps":[x.model_dump() for x in steps],"completed_goals":list(goals),"deferred_goals":list(deferred),"mapping":dict(mapping)})
    stage("Learning what data to read")
    for field in ["total_fare", "currency", "airline", "flight_number", "origin", "destination", "departure_time", "arrival_time"]:
        state = await browser.page_state(request, field, scope=cards, expanded=True)
        if not state:
            await guard()
            raise DomainError("RECIPE_CONTEXT_INSUFFICIENT", "Safe result-field context is unavailable.")
        correction = ''
        for mapping_attempt in range(3):
            selected = await choice(
                'Map ' + field + ' from this one observed result card for the requested ' + request.cabin + ' cabin using '
                + ('extract_money' if field == 'total_fare' else 'extract_text') + '. ' + correction,
                state, actions=['extract_money' if field == 'total_fare' else 'extract_text'])
            if selected.status != 'ACTION':
                break
            node = next(x for x in state['elements'] if x['target_id'] == selected.target_id)
            correction = mapping_problem(field, node, browser._planner_targets[selected.target_id], request)
            if not correction:
                break
            record('PLANNER_FIELD_REJECTED', field=field, reason=correction)
        else:
            raise DomainError('RECIPE_FIELD_MAPPING_INVALID', correction)
        if selected.status == "NEED_MORE_CONTEXT" and field in {"departure_time", "arrival_time"}:
            record("PLANNER_FIELD_UNAVAILABLE", field=field)
            continue
        if selected.status != "ACTION" or selected.action != (
            "extract_money" if field == "total_fare" else "extract_text"
        ):
            raise DomainError(
                "RECIPE_GENERATION_FAILED", "AI could not map the required " + field.replace("_", " ") + "."
            )
        mapping[field] = browser._planner_targets[selected.target_id]["selector"]
        if field == "airline" and browser._planner_targets[selected.target_id]["tag"] == "img":
            attributes[field] = "alt"
        record("PLANNER_FIELD_MAPPED", field=field, target_id=selected.target_id)
        persist({"phase":"FIELDS","steps":[x.model_dump() for x in steps],"completed_goals":list(goals),"deferred_goals":list(deferred),"mapping":dict(mapping)})
    # Trusted route/date/cabin/passenger context validation remains in Eura.
    manifest = Manifest(
        contract=2,
        generated=True,
        source_slug="pending",
        version="pending",
        allowed_hosts=list(browser.allowed_hosts),
        search_ready=initial_selector,
        result_ready=cards,
        empty_results=":not(*)",
        context_selector="body",
        search_steps=[],
        planner_steps=steps,
        validated_search={
            key: str(getattr(request, key))
            for key in ["origin", "destination", "departure_date", "adults", "children", "infants", "cabin"]
        },
        single_adult_only=True,
        field_attributes=attributes,
        selectors={k:v for k,v in mapping.items() if k not in {"origin","destination"}},
        route_selectors={k:mapping[k] for k in ["origin","destination"]},
    )
    persist(manifest.model_dump())
    structure = await browser.structure()
    stage("Testing recipe")
    await isolate({"selectors": manifest.selectors, "stability_ms": manifest.stability_ms}, structure or [])
    await memory.remember(
        source_id, "mapping", "fare fields", {"fields": mapping, "state": "AWAITING_LIVE_VALIDATION"}
    )
    from udaan.eura_engine import EuraEngine
    EuraEngine().validate_structure(structure or [], manifest.selectors)
    return manifest, structure
