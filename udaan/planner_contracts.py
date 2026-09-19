"""Small planner DSL. Browser-discovered IDs are the only model-selected targets."""

from typing import Literal

from pydantic import Field, model_validator

from udaan.contracts import Strict

ACTIONS = Literal[
    "click",
    "fill",
    "select",
    "press",
    "scroll",
    "wait_visible",
    "wait_text",
    "wait_url",
    "extract_text",
    "extract_money",
]


class NextAction(Strict):
    status: Literal["ACTION", "NEED_MORE_CONTEXT", "MANUAL_ACTION_REQUIRED"]
    action: ACTIONS | None = None
    target_id: str | None = Field(None, pattern=r"^[ec][0-9]{1,4}$")
    value: str | None = Field(None, max_length=80)
    reason: str | None = Field(None, max_length=160)

    @model_validator(mode="after")
    def bounded(self):
        if self.status == "ACTION":
            if not self.action or not self.target_id:
                raise ValueError("One action and an observed target_id are required")
            if self.action in {"fill", "select"} and self.value not in {
                "{{origin}}",
                "{{destination}}",
                "{{departure_date}}",
                "{{adults}}",
                "{{children}}",
                "{{infants}}",
                "{{cabin}}",
            }:
                raise ValueError("Input values must use a supplied search parameter template")
            if self.action == "press" and self.value not in {
                "Enter",
                "Tab",
                "Escape",
                "ArrowDown",
                "ArrowUp",
            }:
                raise ValueError("Only ordinary search-navigation keys are permitted")
            if self.action not in {"fill", "select", "press"} and self.value is not None:
                raise ValueError("This action takes no model-supplied value")
        elif self.action or self.target_id or self.value:
            raise ValueError("A non-action response must not contain an executable action")
        return self


class LearnedAction(Strict):
    action: ACTIONS
    selector: str = Field(max_length=500)
    goal: Literal["origin", "destination", "departure_date", "adults", "cabin", "submit"]
    value: str | None = Field(None, max_length=80)
    label: str = Field("", max_length=160)
    tag: str = Field(max_length=20)
    calendar_day: bool = False
    verified: bool
