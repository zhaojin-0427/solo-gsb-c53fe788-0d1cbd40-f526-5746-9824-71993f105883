"""Request schemas and model validation for the compatibility checker.

All structural *and* semantic model errors are raised as pydantic validation
errors so that FastAPI answers them with HTTP 422 automatically.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX32 = 2**32 - 1


class Term(BaseModel):
    """One summand of a linear expression: ``coef * reg`` or ``coef * param``."""

    model_config = ConfigDict(extra="forbid")

    reg: Optional[str] = Field(default=None, min_length=1)
    param: Optional[Literal[True]] = None
    coef: int = 1

    @model_validator(mode="after")
    def _exactly_one_source(self) -> "Term":
        if (self.reg is None) == (self.param is None):
            raise ValueError("each term must reference exactly one of 'reg' or 'param'")
        return self


class GuardAtom(BaseModel):
    """One linear comparison: ``sum(coef * term) <op> const``."""

    model_config = ConfigDict(extra="forbid")

    terms: list[Term] = Field(min_length=1)
    op: Literal["<=", "<", ">=", ">", "==", "!="]
    const: int


class RegisterUpdate(BaseModel):
    """Update of a single register.

    Exactly one of:
      * ``{"set": c}``   assign constant c
      * ``{"param": true}``  take the request parameter
      * ``{"add": c}``   add constant c
      * ``{"sub": c}``   subtract constant c
    If the resulting value would leave the unsigned 32-bit range the
    transition cannot be taken.
    """

    model_config = ConfigDict(extra="forbid")

    set: Optional[int] = Field(default=None, ge=0, le=MAX32)
    param: Optional[Literal[True]] = None
    add: Optional[int] = Field(default=None, ge=0, le=MAX32)
    sub: Optional[int] = Field(default=None, ge=0, le=MAX32)

    @model_validator(mode="after")
    def _exactly_one_kind(self) -> "RegisterUpdate":
        n = sum(v is not None for v in (self.set, self.param, self.add, self.sub))
        if n != 1:
            raise ValueError(
                "a register update must specify exactly one of 'set', 'param', 'add', 'sub'"
            )
        return self


class Response(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: int = Field(ge=100, le=599)
    label: str = Field(min_length=1)


class Transition(BaseModel):
    """One transition.

    With ``request`` present it is a request transition: it consumes a named
    request carrying one unsigned 32-bit integer parameter and must define a
    ``response``.  Without ``request`` it is a silent tau step which may not
    reference the parameter at all.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: Optional[str] = Field(default=None, min_length=1)
    from_: str = Field(alias="from", min_length=1)
    to: str = Field(min_length=1)
    request: Optional[str] = Field(default=None, min_length=1)
    guard: list[GuardAtom] = Field(default_factory=list)
    update: dict[str, RegisterUpdate] = Field(default_factory=dict)
    response: Optional[Response] = None

    @model_validator(mode="after")
    def _check_tau_vs_request(self) -> "Transition":
        if self.request is None:
            if self.response is not None:
                raise ValueError("a tau transition (no 'request') must not define a 'response'")
            for atom in self.guard:
                for term in atom.terms:
                    if term.param is not None:
                        raise ValueError(
                            "a tau transition guard must not reference the request parameter"
                        )
            for upd in self.update.values():
                if upd.param is not None:
                    raise ValueError("a tau transition must not consume the request parameter")
        elif self.response is None:
            raise ValueError("a request transition must define a 'response'")
        return self


class StateModel(BaseModel):
    """A state model: initial control state, at most two unsigned 32-bit
    registers with initial values, and transitions."""

    model_config = ConfigDict(extra="forbid")

    initial_state: str = Field(min_length=1)
    registers: dict[str, int] = Field(default_factory=dict)
    transitions: list[Transition] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_model(self) -> "StateModel":
        if len(self.registers) > 2:
            raise ValueError("at most two 32-bit registers are allowed")
        for name, value in self.registers.items():
            if not name:
                raise ValueError("register names must be non-empty")
            if not (0 <= value <= MAX32):
                raise ValueError(
                    f"initial value of register '{name}' is outside the unsigned 32-bit range"
                )
        known = set(self.registers)
        for tr in self.transitions:
            for reg in tr.update:
                if reg not in known:
                    raise ValueError(f"update of unknown register '{reg}'")
            for atom in tr.guard:
                for term in atom.terms:
                    if term.reg is not None and term.reg not in known:
                        raise ValueError(f"guard references unknown register '{term.reg}'")
        return self


class CompareRequest(BaseModel):
    """Body of POST /compare: the old and the new state model plus the bound
    of requests per trace that is checked (default and maximum: 8)."""

    model_config = ConfigDict(extra="forbid")

    old: StateModel
    new: StateModel
    max_requests: int = Field(default=8, ge=1, le=8)
