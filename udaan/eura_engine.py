"""Eura's optional learning backend; deterministic collection stays in udaan.eura.

Third-party extraction imports belong only here. No crawler, model, downloads or
network calls are started when initializing or checking this engine.
"""
import logging
from functools import lru_cache
from importlib.metadata import version

from udaan.config import settings
from udaan.contracts import DomainError

log = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def backend():
    import os
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    from crawl4ai import JsonCssExtractionStrategy
    return JsonCssExtractionStrategy


class EuraEngine:
    def __init__(self, config=None):
        self.config = config or settings()

    def initialize(self):
        from udaan.eura import Manifest
        try:
            strategy = backend()({"name": "Eura", "baseSelector": "div",
                                  "fields": [{"name": "value", "type": "text", "selector": "span"}]}, verbose=False)
            if strategy.extract("", "<div><span>ready</span></div>") != [{"value": "ready"}]:
                raise ValueError("Extraction check failed")
            Manifest.model_json_schema()
            root = self.config.recipe_directory
            if not root.is_dir():
                raise OSError("Recipe directory missing")
            list(root.iterdir())
        except Exception as exc:
            log.debug("Eura initialization failed", exc_info=True)
            raise DomainError("EURA_UNAVAILABLE", "Eura Engine is unavailable. Check its dependencies and recipe directory in Advanced.") from exc
        return self

    def health(self):
        try:
            self.initialize()
            return {"status": "READY", "message": "Eura Engine and recipes are ready.",
                    "advanced": {"backend": "Crawl4AI", "version": version("crawl4ai")}}
        except DomainError as exc:
            return {"status": "NOT READY", "message": exc.message}

    def extract(self, html, selectors):
        """Read an already captured page; never open a second browser or call AI."""
        from udaan.eura import RecipePatch
        RecipePatch(selectors=selectors)
        schema = {"name": "Eura recipe", "baseSelector": selectors["cards"],
                  "fields": [{"name": key, "selector": value, "type": "text"}
                             for key, value in selectors.items() if key != "cards"]}
        try:
            return backend()(schema, verbose=False).extract("", html)
        except Exception as exc:
            log.debug("Eura extraction failed", exc_info=True)
            raise DomainError("EURA_EXTRACTION_FAILED", "Eura could not understand the page.") from exc

    def validate_structure(self, structure, selectors):
        """Check the observed card structure before the existing live fare gates."""
        from lxml import etree
        root, nodes = etree.Element("main"), []
        for index, item in enumerate(structure[:1500]):
            parent = item.get("parent")
            if parent is not None and (not isinstance(parent, int) or not 0 <= parent < index):
                raise DomainError("RECIPE_INVALID", "Eura could not read the page information.")
            node = etree.SubElement(root if parent is None else nodes[parent], item["tag"])
            nodes.append(node)
            node.set("data-eura-node", str(index))
            node.set("class", " ".join(item.get("classes", [])))
            for key, attr in [("id", "id"), ("role", "role"), ("type", "type"), ("aria_label", "aria-label")]:
                if item.get(key):
                    node.set(attr, item[key])
            node.text = item.get("label", "")
        from udaan.eura import RecipePatch
        RecipePatch(selectors=selectors)
        schema = {"name": "Eura structure", "baseSelector": selectors["cards"], "fields": [],
                  "baseFields": [{"name": "node", "type": "attribute", "attribute": "data-eura-node"}]}
        try:
            rows = backend()(schema, verbose=False).extract("", etree.tostring(root, encoding="unicode"))
        except Exception as exc:
            log.debug("Eura structure check failed", exc_info=True)
            raise DomainError("EURA_EXTRACTION_FAILED", "Eura could not understand the page.") from exc
        if not rows:
            raise DomainError("RECIPE_INVALID", "Eura could not find flight results in the page information.")

    async def repair(self, provider, context):
        import json
        self.initialize()
        try:
            payload = json.loads(context) if isinstance(context, str) else context
            self.validate_structure(payload["structure"], payload["manifest"]["selectors"])
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise DomainError("DIAGNOSTICS_UNAVAILABLE", "Eura repair requires captured page structure") from exc
        return await provider.generate(context)


class EuraRecipeBuilder(EuraEngine):
    async def learn(self, *args, **kwargs):
        self.initialize()
        from udaan.planner import learn
        return await learn(*args, **kwargs)


def recipe_summary(db, source):
    """Product facts from persisted recipe, test and repair records."""
    from sqlalchemy import select

    from udaan.db import Job, Recipe, Repair
    recipe = db.get(Recipe, source.active_recipe_id) if source.active_recipe_id else None
    if recipe is None:
        recipe = db.scalar(select(Recipe).where(Recipe.source_id == source.id).order_by(Recipe.created_at.desc()).limit(1))
    if recipe is None:
        return {"recipe_id": None, "version": None, "status": "NO RECIPE", "built_by": None,
                "last_tested": None, "last_repair": None, "message": "No working recipe."}
    last_test = db.scalar(select(Job).where(Job.recipe_id == recipe.id, Job.finished_at.is_not(None),
        Job.purpose.in_(["RECIPE_VALIDATION", "REPAIR_VALIDATION", "RECIPE_BUILD"]))
        .order_by(Job.finished_at.desc()).limit(1))
    repair = db.scalar(select(Repair).where(Repair.recipe_id == recipe.id).order_by(Repair.created_at.desc()).limit(1))
    promoted = db.scalar(select(Repair).where(Repair.candidate_recipe_id == recipe.id, Repair.state == "PROMOTED")
        .order_by(Repair.created_at.desc()).limit(1))
    return {"recipe_id": recipe.id, "version": recipe.version,
            "status": "READY" if recipe.health == "HEALTHY" and recipe.live_validated_at else
                      "DEGRADED" if recipe.health == "DEGRADED" else "NEEDS TEST",
            "built_by": "Repaired" if promoted else "Eura AI" if recipe.manifest.get("generated") else "Manual",
            "last_tested": last_test.finished_at if last_test else recipe.live_validated_at,
            "last_repair": ((promoted or repair).history[-1].get("at") if (promoted or repair).history else (promoted or repair).created_at) if promoted or repair else None,
            "repair": {"id": repair.id, "state": repair.state} if repair else None}
