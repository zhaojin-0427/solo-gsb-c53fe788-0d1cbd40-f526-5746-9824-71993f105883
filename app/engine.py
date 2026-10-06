"""Bounded trace-inclusion checking between an old and a new state model.

Semantics
---------
* One request cycle of a model is: up to two tau transitions followed by
  exactly one request transition ("at most two tau steps before each
  request").
* A request transition consumes a named request carrying one unsigned 32-bit
  integer parameter and produces a ``(status, label)`` response.
* Tau transitions are silent: no request, no response, and they may not
  reference the request parameter.
* A register update whose result would leave the unsigned 32-bit range makes
  the transition unavailable ("out-of-bounds updates cannot be taken").

The checker verifies that every request/response trace of the old model with
up to ``max_requests`` requests can be reproduced by the new model (including
the case of different responses to the same request).

Algorithm (only quantifier-free linear integer arithmetic):
for each trace length k = 1..max_requests

1. the new model is explored symbolically for k cycles.  Because register
   updates can only set a constant, copy the parameter, or add/subtract a
   constant, every symbolic register value has the canonical form ``const``
   or ``param_i + const``; configurations are merged by (control state,
   canonical register values), keeping the explored set small.  The result
   is a formula ``M(obs, params)`` describing exactly the observable traces
   the new model can reproduce.
2. an optimizer looks for the lexicographically smallest old trace (ordered
   per step by request name, parameter, status code, label) satisfying
   ``old trace and not M``.  If none exists there is no counterexample of
   this length; otherwise the model found is the shortest, lexicographically
   smallest counterexample.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from z3 import And, Implies, Int, IntVal, Not, Optimize, Or, sat, unknown

from .schemas import MAX32, CompareRequest, StateModel, Transition

SOLVER_TIMEOUT_MS = 10_000
MAX_LISTED_CONFIGS = 100


class InconclusiveError(RuntimeError):
    """The solver could not decide a query within the time limit."""


# ---------------------------------------------------------------------------
# shared comparison helpers
# ---------------------------------------------------------------------------

_OPS = {
    "<=": lambda a, b: a <= b,
    "<": lambda a, b: a < b,
    ">=": lambda a, b: a >= b,
    ">": lambda a, b: a > b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}


def _z3_cmp(op: str, lhs, rhs):
    if op == "<=":
        return lhs <= rhs
    if op == "<":
        return lhs < rhs
    if op == ">=":
        return lhs >= rhs
    if op == ">":
        return lhs > rhs
    if op == "==":
        return lhs == rhs
    return lhs != rhs


# ---------------------------------------------------------------------------
# concrete (pure Python) evaluation, used for path / reachability reporting
# ---------------------------------------------------------------------------


def eval_guard_py(guard, regs: dict, param: Optional[int]) -> bool:
    for atom in guard:
        lhs = 0
        for term in atom.terms:
            value = regs[term.reg] if term.reg is not None else param
            lhs += term.coef * value
        if not _OPS[atom.op](lhs, atom.const):
            return False
    return True


def apply_update_py(update, regs: dict, param: Optional[int]) -> Optional[dict]:
    """Apply a register update; return None if any result is out of bounds."""
    out = dict(regs)
    for reg, upd in update.items():
        if upd.set is not None:
            value = upd.set
        elif upd.param is not None:
            value = param
        elif upd.add is not None:
            value = regs[reg] + upd.add
        else:
            value = regs[reg] - upd.sub
        if not (0 <= value <= MAX32):
            return None
        out[reg] = value
    return out


# ---------------------------------------------------------------------------
# compiled model
# ---------------------------------------------------------------------------


class CompiledModel:
    def __init__(self, model: StateModel):
        self.model = model
        self.registers = list(model.registers.keys())
        self.regpos = {r: i for i, r in enumerate(self.registers)}
        self.initial_registers = dict(model.registers)
        states = {model.initial_state}
        for tr in model.transitions:
            states.add(tr.from_)
            states.add(tr.to)
        self.state_index = {s: i for i, s in enumerate(sorted(states))}
        # (index in model.transitions, transition), split by kind
        self.tau = [(i, t) for i, t in enumerate(model.transitions) if t.request is None]
        self.req = [(i, t) for i, t in enumerate(model.transitions) if t.request is not None]
        self.request_names = sorted({t.request for _, t in self.req})
        self.labels = sorted({t.response.label for _, t in self.req})


# ---------------------------------------------------------------------------
# old-side bounded unrolling (z3)
# ---------------------------------------------------------------------------


def _z3_guard(guard, regvars: dict, param):
    conds = []
    for atom in guard:
        lhs = 0
        for term in atom.terms:
            var = regvars[term.reg] if term.reg is not None else param
            lhs = lhs + term.coef * var
        conds.append(_z3_cmp(atom.op, lhs, atom.const))
    return And(*conds)


def _z3_update(update, regvars: dict, param):
    """Return (bounds constraints, {register: new value expression})."""
    constraints, new_values = [], {}
    for reg, current in regvars.items():
        upd = update.get(reg)
        if upd is None:
            new_values[reg] = current
            continue
        if upd.set is not None:
            expr = IntVal(upd.set)
        elif upd.param is not None:
            expr = param
        elif upd.add is not None:
            expr = current + upd.add
        else:
            expr = current - upd.sub
        constraints += [expr >= 0, expr <= MAX32]
        new_values[reg] = expr
    return constraints, new_values


@dataclass
class Encoding:
    constraints: list
    choice: list  # choice variable per slot (-1 = skipped tau slot)
    params: list
    names: list
    statuses: list
    labels: list


def encode_old(cm: CompiledModel, k: int, name_rank: dict, label_rank: dict) -> Encoding:
    """Unroll ``k`` request cycles (tau, tau, request each) of the old model."""
    nslots = 3 * k
    states = [Int(f"ost{j}") for j in range(nslots + 1)]
    regs = [{r: Int(f"or{j}_{r}") for r in cm.registers} for j in range(nslots + 1)]
    choice = [Int(f"oc{j}") for j in range(nslots)]
    params = [Int(f"op{i}") for i in range(k)]
    names = [Int(f"on{i}") for i in range(k)]
    statuses = [Int(f"os{i}") for i in range(k)]
    labels = [Int(f"ol{i}") for i in range(k)]

    constraints = [states[0] == cm.state_index[cm.model.initial_state]]
    constraints += [regs[0][r] == cm.initial_registers[r] for r in cm.registers]

    for i in range(k):
        constraints += [params[i] >= 0, params[i] <= MAX32]
        for pos in range(2):  # the two optional tau slots of cycle i
            j = 3 * i + pos
            constraints.append(Or([choice[j] == -1] + [choice[j] == t for t in range(len(cm.tau))]))
            for t, (_, tr) in enumerate(cm.tau):
                bounds, new_values = _z3_update(tr.update, regs[j], None)
                constraints.append(Implies(choice[j] == t, And(
                    states[j] == cm.state_index[tr.from_],
                    _z3_guard(tr.guard, regs[j], None),
                    *bounds,
                    states[j + 1] == cm.state_index[tr.to],
                    *[regs[j + 1][r] == new_values[r] for r in cm.registers],
                )))
            constraints.append(Implies(choice[j] == -1, And(
                states[j + 1] == states[j],
                *[regs[j + 1][r] == regs[j][r] for r in cm.registers],
            )))
        j = 3 * i + 2  # the request slot of cycle i
        allowed = list(range(len(cm.req)))
        constraints.append(Or([choice[j] == t for t in allowed]))
        for t in allowed:
            tr = cm.req[t][1]
            bounds, new_values = _z3_update(tr.update, regs[j], params[i])
            constraints.append(Implies(choice[j] == t, And(
                states[j] == cm.state_index[tr.from_],
                _z3_guard(tr.guard, regs[j], params[i]),
                *bounds,
                states[j + 1] == cm.state_index[tr.to],
                *[regs[j + 1][r] == new_values[r] for r in cm.registers],
                names[i] == name_rank[tr.request],
                statuses[i] == tr.response.status,
                labels[i] == label_rank[tr.response.label],
            )))
    return Encoding(constraints, choice, params, names, statuses, labels)


# ---------------------------------------------------------------------------
# new-side symbolic coverage
# ---------------------------------------------------------------------------
#
# A symbolic register value is a pair (base, offset):
#   base is None  -> the constant ``offset``
#   base is i     -> ``param_i + offset``
# Configurations are merged by (control state, symbolic register values);
# the condition of a merged configuration is the disjunction of the path
# conditions that reach it.


def _sym_guard(guard, symregs, cm: CompiledModel, enc: Optional[Encoding],
               cycle: Optional[int]):
    """Guard over symbolic registers; False if statically unsatisfiable."""
    conds = []
    for atom in guard:
        coeffs: dict[int, int] = {}
        const = 0
        for term in atom.terms:
            if term.reg is not None:
                base, offset = symregs[cm.regpos[term.reg]]
                const += term.coef * offset
                if base is not None:
                    coeffs[base] = coeffs.get(base, 0) + term.coef
            else:
                coeffs[cycle] = coeffs.get(cycle, 0) + term.coef
        if not coeffs:
            if not _OPS[atom.op](const, atom.const):
                return False
            continue
        lhs = const
        for idx, coef in coeffs.items():
            lhs = lhs + coef * enc.params[idx]
        conds.append(_z3_cmp(atom.op, lhs, atom.const))
    return And(*conds)


def _sym_update(update, symregs, cm: CompiledModel, enc: Optional[Encoding],
                cycle: Optional[int]):
    """Symbolic register update; None if statically out of bounds."""
    new_regs = list(symregs)
    conds = []
    for reg, upd in update.items():
        idx = cm.regpos[reg]
        base, offset = symregs[idx]
        if upd.set is not None:
            new_base, new_offset = None, upd.set
        elif upd.param is not None:
            new_base, new_offset = cycle, 0
        elif upd.add is not None:
            new_base, new_offset = base, offset + upd.add
        else:
            new_base, new_offset = base, offset - upd.sub
        if new_base is None:
            if not (0 <= new_offset <= MAX32):
                return None
        else:
            param = enc.params[new_base]
            if new_offset < 0:
                conds.append(param >= -new_offset)
            if new_offset > 0:
                conds.append(param <= MAX32 - new_offset)
        new_regs[idx] = (new_base, new_offset)
    return tuple(new_regs), And(*conds)


def _sym_tau_closure(cm: CompiledModel, configs: dict, enc: Encoding) -> dict:
    """All configurations reachable with 0, 1 or 2 tau steps."""
    result = dict(configs)
    frontier = dict(configs)
    for _ in range(2):  # at most two tau steps before each request
        nxt: dict = {}
        for (state, symregs), cond in frontier.items():
            for _, tr in cm.tau:
                if tr.from_ != state:
                    continue
                guard = _sym_guard(tr.guard, symregs, cm, enc, None)
                if guard is False:
                    continue
                updated = _sym_update(tr.update, symregs, cm, enc, None)
                if updated is None:
                    continue
                new_regs, bounds = updated
                key = (tr.to, new_regs)
                cond_new = And(cond, guard, bounds)
                nxt[key] = Or(nxt[key], cond_new) if key in nxt else cond_new
        for key, cond_new in nxt.items():
            result[key] = Or(result[key], cond_new) if key in result else cond_new
        frontier = nxt
        if not frontier:
            break
    return result


def _sym_request_step(cm: CompiledModel, configs: dict, i: int, enc: Encoding,
                      name_rank: dict, label_rank: dict) -> dict:
    result: dict = {}
    for (state, symregs), cond in configs.items():
        for _, tr in cm.req:
            if tr.from_ != state:
                continue
            if tr.request not in name_rank or tr.response.label not in label_rank:
                continue  # can never reproduce an old observable
            guard = _sym_guard(tr.guard, symregs, cm, enc, i)
            if guard is False:
                continue
            updated = _sym_update(tr.update, symregs, cm, enc, i)
            if updated is None:
                continue
            new_regs, bounds = updated
            cond_new = And(
                cond, guard, bounds,
                enc.names[i] == name_rank[tr.request],
                enc.statuses[i] == tr.response.status,
                enc.labels[i] == label_rank[tr.response.label],
            )
            key = (tr.to, new_regs)
            result[key] = Or(result[key], cond_new) if key in result else cond_new
    return result


def build_new_coverage(new_cm: CompiledModel, k: int, enc: Encoding,
                       name_rank: dict, label_rank: dict):
    """Formula over ``enc`` observables/params characterising exactly the
    traces of length ``k`` that the new model can reproduce."""
    init = tuple((None, new_cm.initial_registers[r]) for r in new_cm.registers)
    configs = {(new_cm.model.initial_state, init): True}
    for i in range(k):
        configs = _sym_tau_closure(new_cm, configs, enc)
        configs = _sym_request_step(new_cm, configs, i, enc, name_rank, label_rank)
        if not configs:
            break
    return Or(*configs.values())


# ---------------------------------------------------------------------------
# counterexample search
# ---------------------------------------------------------------------------


@dataclass
class Counterexample:
    params: list
    observables: list  # (request name, status, label) per step
    old_choices: list  # old-side transition choice per slot


@dataclass
class Context:
    old: CompiledModel
    new: CompiledModel
    name_rank: dict
    label_rank: dict
    name_of_rank: dict
    label_of_rank: dict


def find_counterexample(ctx: Context, k: int) -> Optional[Counterexample]:
    enc = encode_old(ctx.old, k, ctx.name_rank, ctx.label_rank)
    coverage = build_new_coverage(ctx.new, k, enc, ctx.name_rank, ctx.label_rank)
    opt = Optimize()
    opt.set("timeout", SOLVER_TIMEOUT_MS)
    opt.set("priority", "lex")
    opt.add(*enc.constraints)
    opt.add(Not(coverage))
    for i in range(k):  # lexicographic tie-break: name, param, status, label
        opt.minimize(enc.names[i])
        opt.minimize(enc.params[i])
        opt.minimize(enc.statuses[i])
        opt.minimize(enc.labels[i])
    result = opt.check()
    if result == unknown:
        raise InconclusiveError(f"solver could not decide the query for trace length {k}")
    if result != sat:
        return None  # no counterexample of this length
    model = opt.model()
    params = [model.eval(enc.params[i]).as_long() for i in range(k)]
    observables = [(
        ctx.name_of_rank[model.eval(enc.names[i]).as_long()],
        model.eval(enc.statuses[i]).as_long(),
        ctx.label_of_rank[model.eval(enc.labels[i]).as_long()],
    ) for i in range(k)]
    old_choices = [model.eval(enc.choice[j]).as_long() for j in range(len(enc.choice))]
    return Counterexample(params, observables, old_choices)


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------


def transition_label(index: int, tr: Transition) -> str:
    return tr.id if tr.id is not None else f"transitions[{index}]"


def old_path(old_cm: CompiledModel, cex: Counterexample) -> dict:
    """Replay the old-side counterexample path concretely."""
    steps = []
    state = old_cm.model.initial_state
    regs = dict(old_cm.initial_registers)
    k = len(cex.params)
    for i in range(k):
        for pos in range(2):
            chosen = cex.old_choices[3 * i + pos]
            if chosen == -1:
                continue
            gidx, tr = old_cm.tau[chosen]
            regs = apply_update_py(tr.update, regs, None)
            steps.append({
                "kind": "tau",
                "transition": transition_label(gidx, tr),
                "from": state,
                "to": tr.to,
                "registers_after": regs,
            })
            state = tr.to
        gidx, tr = old_cm.req[cex.old_choices[3 * i + 2]]
        param = cex.params[i]
        regs = apply_update_py(tr.update, regs, param)
        steps.append({
            "kind": "request",
            "transition": transition_label(gidx, tr),
            "request": tr.request,
            "param": param,
            "response": {"status": tr.response.status, "label": tr.response.label},
            "from": state,
            "to": tr.to,
            "registers_after": regs,
        })
        state = tr.to
    return {
        "initial": {"state": old_cm.model.initial_state,
                    "registers": dict(old_cm.initial_registers)},
        "steps": steps,
    }


def _render_configs(cm: CompiledModel, configs) -> tuple[list, bool]:
    rendered = [
        {"state": st, "registers": {r: v for r, v in zip(cm.registers, values)}}
        for st, values in sorted(configs)
    ]
    return rendered[:MAX_LISTED_CONFIGS], len(rendered) > MAX_LISTED_CONFIGS


def new_reachable(new_cm: CompiledModel, trace: list) -> list:
    """Step-by-step reachable states of the new model while trying to follow
    the counterexample trace (concrete parameters)."""
    def key(state, regs):
        return state, tuple(regs[r] for r in new_cm.registers)

    def tau_closure(configs):
        reached = set(configs)
        frontier = set(configs)
        for _ in range(2):  # at most two tau steps before each request
            nxt = set()
            for st, values in frontier:
                regs = dict(zip(new_cm.registers, values))
                for _, tr in new_cm.tau:
                    if tr.from_ != st or not eval_guard_py(tr.guard, regs, None):
                        continue
                    new_regs = apply_update_py(tr.update, regs, None)
                    if new_regs is not None:
                        nxt.add(key(tr.to, new_regs))
            nxt -= reached
            reached |= nxt
            frontier = nxt
            if not frontier:
                break
        return reached

    configs = {key(new_cm.model.initial_state, new_cm.initial_registers)}
    report = []
    for i, step in enumerate(trace):
        before = tau_closure(configs)
        after, responses = set(), set()
        for st, values in before:
            regs = dict(zip(new_cm.registers, values))
            for _, tr in new_cm.req:
                if tr.from_ != st or tr.request != step["request"]:
                    continue
                if not eval_guard_py(tr.guard, regs, step["param"]):
                    continue
                new_regs = apply_update_py(tr.update, regs, step["param"])
                if new_regs is None:
                    continue
                after.add(key(tr.to, new_regs))
                responses.add((tr.response.status, tr.response.label))
        before_list, before_trunc = _render_configs(new_cm, before)
        after_list, after_trunc = _render_configs(new_cm, after)
        entry = {
            "step": i + 1,
            "request": step["request"],
            "param": step["param"],
            "expected_response": {"status": step["status"], "label": step["label"]},
            "response_reproduced": (step["status"], step["label"]) in responses,
            "states_before_request": before_list,
            "new_possible_responses": [
                {"status": s, "label": l} for s, l in sorted(responses)
            ],
            "states_after_request": after_list,
        }
        if before_trunc:
            entry["states_before_request_truncated"] = True
        if after_trunc:
            entry["states_after_request_truncated"] = True
        report.append(entry)
        configs = after
    return report


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def check_compatibility(payload: CompareRequest) -> dict:
    ctx = Context(
        old=CompiledModel(payload.old),
        new=CompiledModel(payload.new),
        name_rank={}, label_rank={}, name_of_rank={}, label_of_rank={},
    )
    ctx.name_rank = {n: i for i, n in enumerate(ctx.old.request_names)}
    ctx.label_rank = {l: i for i, l in enumerate(ctx.old.labels)}
    ctx.name_of_rank = {i: n for n, i in ctx.name_rank.items()}
    ctx.label_of_rank = {i: l for l, i in ctx.label_rank.items()}

    for k in range(1, payload.max_requests + 1):
        cex = find_counterexample(ctx, k)
        if cex is None:
            continue
        trace = [
            {"request": name, "param": param, "status": status, "label": label}
            for (name, status, label), param in zip(cex.observables, cex.params)
        ]
        return {
            "result": "incompatible",
            "checked_requests": payload.max_requests,
            "counterexample": {"length": k, "trace": trace},
            "old_path": old_path(ctx.old, cex),
            "new_reachable": new_reachable(ctx.new, trace),
        }
    return {
        "result": "compatible",
        "checked_requests": payload.max_requests,
        "detail": (
            f"Every request/response trace of the old model with up to "
            f"{payload.max_requests} request(s) is reproducible by the new model. "
            f"This is a bounded result only: behaviour beyond "
            f"{payload.max_requests} request(s) was not checked."
        ),
    }
