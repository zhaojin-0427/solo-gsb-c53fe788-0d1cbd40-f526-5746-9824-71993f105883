"""Request/response schemas and model validation for the compatibility checker.

All semantic model errors are raised as pydantic ValidationError, which FastAPI
turns into HTTP 422 automatically.
"""

from __future__ import annotations

from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

U32_MAX = 2**32 - 1

GUARD_OPS = {"<", "<=", "==", "!=", ">=", ">"}


class Term(BaseModel):
    """One linear term: either a register reference or the request parameter."""

    reg: Optional[str] = None
    param: Optional[bool] = None
    coef: int = 1

    @model_validator(mode="after")
    def _check_exactly_one_operand(self) -> "Term":
        if (self.reg is None) == (self.param is not True):
            raise ValueError("each term must set exactly one of 'reg' or 'param: true'")
        return self


class Comparison(BaseModel):
    """sum(coef * term) <op> const, e.g. x - param <= 0."""

    terms: List[Term] = Field(default_factory=list)
    op: str
    const: int = 0

    @field_validator("op")
    @classmethod
    def _check_op(cls, v: str) -> str:
        if v not in GUARD_OPS:
            raise ValueError(f"op must be one of {sorted(GUARD_OPS)}")
        return v


class Update(BaseModel):
    """Register update: assign constant / take parameter / add / sub constant."""

    kind: Literal["const", "param", "add", "sub"]
    value: Optional[int] = None

    @model_validator(mode="after")
    def _check_value(self) -> "Update":
        if self.kind == "param":
            if self.value is not None:
                raise ValueError("update of kind 'param' must not have a value")
        else:
            if self.value is None:
                raise ValueError(f"update of kind '{self.kind}' requires a value")
            if not (0 <= self.value <= U32_MAX):
                raise ValueError(
                    f"update value must be within [0, {U32_MAX}] (32-bit unsigned)"
                )
        return self


class Response(BaseModel):
    status: int = Field(ge=100, le=599)
    label: str = Field(min_length=1)


class Transition(BaseModel):
    """One transition.

    With 'request' set it consumes that named request and must carry a
    'response' (status code + label).  Without 'request' it is a tau step and
    must neither reference the parameter nor produce a response.
    """

    model_config = {"populate_by_name": True}

    id: Optional[str] = None
    from_state: str = Field(alias="from", min_length=1)
    to_state: str = Field(alias="to", min_length=1)
    request: Optional[str] = None
    guard: List[Comparison] = Field(default_factory=list)
    update: Dict[str, Update] = Field(default_factory=dict)
    response: Optional[Response] = None

    @property
    def is_tau(self) -> bool:
        return self.request is None


class StateModel(BaseModel):
    """A stateful interface model: initial state, <=2 uint32 registers, transitions."""

    initial_state: str = Field(min_length=1)
    registers: Dict[str, int] = Field(default_factory=dict)
    transitions: List[Transition] = Field(default_factory=list)

    @field_validator("registers")
    @classmethod
    def _check_registers(cls, v: Dict[str, int]) -> Dict[str, int]:
        if len(v) > 2:
            raise ValueError("at most two registers are allowed")
        for name, val in v.items():
            if not name.isidentifier():
                raise ValueError(f"invalid register name: {name!r}")
            if not (0 <= val <= U32_MAX):
                raise ValueError(
                    f"register {name!r} initial value must be within [0, {U32_MAX}]"
                )
        return v

    @model_validator(mode="after")
    def _check_transitions(self) -> "StateModel":
        reg_names = set(self.registers)
        seen_ids: set = set()
        for i, t in enumerate(self.transitions):
            if t.id is None:
                t.id = f"t{i}"
            if t.id in seen_ids:
                raise ValueError(f"duplicate transition id: {t.id!r}")
            seen_ids.add(t.id)

            if t.is_tau:
                if t.response is not None:
                    raise ValueError(
                        f"transition {t.id!r}: tau step must not have a response"
                    )
            else:
                if t.request == "":
                    raise ValueError(f"transition {t.id!r}: request name must be non-empty")
                if t.response is None:
                    raise ValueError(
                        f"transition {t.id!r}: request transition must have a response"
                    )

            for cmp in t.guard:
                for term in cmp.terms:
                    if term.reg is not None and term.reg not in reg_names:
                        raise ValueError(
                            f"transition {t.id!r}: guard references unknown register "
                            f"{term.reg!r}"
                        )
                    if term.param and t.is_tau:
                        raise ValueError(
                            f"transition {t.id!r}: tau step must not reference the "
                            "request parameter"
                        )

            for reg, upd in t.update.items():
                if reg not in reg_names:
                    raise ValueError(
                        f"transition {t.id!r}: update of unknown register {reg!r}"
                    )
                if upd.kind == "param" and t.is_tau:
                    raise ValueError(
                        f"transition {t.id!r}: tau step must not use the request "
                        "parameter"
                    )
        return self


class CompareRequest(BaseModel):
    old: StateModel
    new: StateModel
    max_requests: int = Field(default=8, ge=1, le=8)
