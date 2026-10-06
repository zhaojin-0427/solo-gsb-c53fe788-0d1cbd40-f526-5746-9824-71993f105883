"""Bounded trace-inclusion engine.

Checks whether every request/response trace of the old model with up to
``max_requests`` requests can be reproduced by the new model.

Semantics
---------
* A run is a sequence of rounds; each round is 0..2 tau steps followed by one
  request transition ("at most two tau steps before each request").
* A transition is enabled iff its guard holds and every register update stays
  inside [0, 2**32 - 1] (out-of-bounds updates cannot be taken).
* The observable of a request transition is (request name, parameter, status,
  label).  The parameter is a single 32-bit unsigned integer shared by both
  models (they observe the same trace).

Algorithm
---------
Symbolic BFS over the determinised product.  A node holds the set of old
configurations and the set of new configurations reachable after one fixed
observable sequence, represented as symbolic runs: (control state, register
expressions over the per-round parameter variables p_0..p_d, path constraint).
At each node we ask z3 whether the old side can emit an observable
a = (r, p, s, l) that the new side cannot match; the first BFS level with such
a divergence yields the shortest counterexamples, and z3.Optimize with
lexicographic objectives minimises (p_0..p_d) so the reported trace is the
lexicographically smallest one by (request, parameter, status, label).
"""

from __future__ import annotations

import time
import z3

from .schemas import U32_MAX, StateModel, Transition

# Safety valves; the CI-class models this service targets finish in well
# under a second, these only guard against pathological inputs.
MAX_NODES = 20000
MAX_RUNS_PER_SIDE = 20000
TIME_BUDGET_SECONDS = 25.0
SOLVER_TIMEOUT_MS = 5000


class EngineError(Exception):
    """Raised when the analysis cannot be completed (resource limits)."""


# --------------------------------------------------------------------------
# z3 helpers
# --------------------------------------------------------------------------

_OPS = {
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    ">=": lambda a, b: a >= b,
    ">": lambda a, b: a > b,
}


def guard_z3(t: Transition, regs: dict, param) -> z3.BoolRef:
    conds = []
    for cmp in t.guard:
        lhs = z3.Sum(
            *[term.coef * (param if term.param else regs[term.reg]) for term in cmp.terms]
        )
        conds.append(_OPS[cmp.op](lhs, cmp.const))
    return z3.And(*conds)


def update_z3(t: Transition, regs: dict, param):
    """Return (new_regs, bounds_condition).  Out-of-bounds => not enabled."""
    new_regs = dict(regs)
    bounds = []
    for name, u in t.update.items():
        cur = regs[name]
        if u.kind == "const":
            new_regs[name] = z3.IntVal(u.value)
        elif u.kind == "param":
            new_regs[name] = param
        elif u.kind == "add":
            new_regs[name] = cur + u.value
            bounds.append(new_regs[name] <= U32_MAX)
        elif u.kind == "sub":
            new_regs[name] = cur - u.value
            bounds.append(new_regs[name] >= 0)
    return new_regs, z3.And(*bounds)


def enabled_z3(t: Transition, regs: dict, param) -> z3.BoolRef:
    _, bounds = update_z3(t, regs, param)
    return z3.And(guard_z3(t, regs, param), bounds)


# --------------------------------------------------------------------------
# Concrete evaluation (for counterexample reconstruction)
# --------------------------------------------------------------------------

def eval_guard(t: Transition, regs: dict, param: int) -> bool:
    for cmp in t.guard:
        lhs = sum(
            term.coef * (param if term.param else regs[term.reg]) for term in cmp.terms
        )
        if cmp.op == "<":
            ok = lhs < cmp.const
        elif cmp.op == "<=":
            ok = lhs <= cmp.const
        elif cmp.op == "==":
            ok = lhs == cmp.const
        elif cmp.op == "!=":
            ok = lhs != cmp.const
        elif cmp.op == ">=":
            ok = lhs >= cmp.const
        else:
            ok = lhs > cmp.const
        if not ok:
            return False
    return True


def eval_update(t: Transition, regs: dict, param: int):
    """Concrete update; returns None if any register would go out of bounds."""
    new_regs = dict(regs)
    for name, u in t.update.items():
        if u.kind == "const":
            v = u.value
        elif u.kind == "param":
            v = param
        elif u.kind == "add":
            v = regs[name] + u.value
        else:
            v = regs[name] - u.value
        if not (0 <= v <= U32_MAX):
            return None
        new_regs[name] = v
    return new_regs


# --------------------------------------------------------------------------
# Machine: one parsed model
# --------------------------------------------------------------------------

class Machine:
    def __init__(self, model: StateModel):
        self.model = model
        self.reg_names = sorted(model.registers)
        self.init_regs = {r: model.registers[r] for r in self.reg_names}
        self.taus_from = {}  # state -> [tau Transition]
        self.obs_from = {}  # state -> {(request, status, label): [Transition]}
        self.observables = set()  # all (request, status, label) keys
        for t in model.transitions:
            if t.is_tau:
                self.taus_from.setdefault(t.from_state, []).append(t)
            else:
                key = (t.request, t.response.status, t.response.label)
                self.obs_from.setdefault(t.from_state, {}).setdefault(key, []).append(t)
                self.observables.add(key)
        self.by_id = {t.id: t for t in model.transitions}

    def initial_run(self) -> "Run":
        return Run(
            state=self.model.initial_state,
            regs={r: z3.IntVal(v) for r, v in self.init_regs.items()},
            constraint=z3.BoolVal(True),
            history=(),
        )


# --------------------------------------------------------------------------
# Symbolic runs
# --------------------------------------------------------------------------

class Run:
    __slots__ = ("state", "regs", "constraint", "history")

    def __init__(self, state, regs, constraint, history):
        self.state = state
        self.regs = regs  # dict name -> z3 expr over parameter variables
        self.constraint = constraint
        self.history = history  # tuple of transition ids

    def key(self, reg_names):
        return (
            self.state,
            tuple(self.regs[r].sexpr() for r in reg_names),
            self.constraint.sexpr(),
        )


class Feasibility:
    """Cached satisfiability checks."""

    def __init__(self):
        self._cache = {}
        self._solver = z3.Solver()
        self._solver.set(timeout=SOLVER_TIMEOUT_MS)

    def sat(self, cond) -> bool:
        key = cond.sexpr()
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        res = self._solver.check(cond)
        if res == z3.unknown:
            raise EngineError("solver returned 'unknown' (timeout)")
        ok = res == z3.sat
        self._cache[key] = ok
        return ok


def _dedup(runs, reg_names):
    seen = set()
    out = []
    for r in runs:
        k = r.key(reg_names)
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


def tau_closure(machine: Machine, runs, feas: Feasibility):
    """All runs reachable by 0, 1 or 2 tau steps (inclusive)."""
    all_runs = list(runs)
    frontier = list(runs)
    for _ in range(2):
        nxt = []
        for run in frontier:
            for t in machine.taus_from.get(run.state, []):
                new_regs, bounds = update_z3(t, run.regs, None)
                c = z3.simplify(
                    z3.And(run.constraint, guard_z3(t, run.regs, None), bounds)
                )
                if z3.is_false(c) or not feas.sat(c):
                    continue
                nxt.append(
                    Run(t.to_state, new_regs, c, run.history + (t.id,))
                )
        all_runs.extend(nxt)
        frontier = nxt
    all_runs = _dedup(all_runs, machine.reg_names)
    if len(all_runs) > MAX_RUNS_PER_SIDE:
        raise EngineError("too many symbolic runs (tau closure exploded)")
    return all_runs


def fire(machine: Machine, pre_runs, obs_key, p_var, feas: Feasibility):
    """Image of pre_runs under request transitions with observable obs_key."""
    out = []
    for run in pre_runs:
        for t in machine.obs_from.get(run.state, {}).get(obs_key, []):
            new_regs, bounds = update_z3(t, run.regs, p_var)
            c = z3.simplify(
                z3.And(
                    run.constraint,
                    guard_z3(t, run.regs, p_var),
                    bounds,
                    p_var >= 0,
                    p_var <= U32_MAX,
                )
            )
            if z3.is_false(c) or not feas.sat(c):
                continue
            out.append(Run(t.to_state, new_regs, c, run.history + (t.id,)))
    out = _dedup(out, machine.reg_names)
    if len(out) > MAX_RUNS_PER_SIDE:
        raise EngineError("too many symbolic runs")
    return out


# --------------------------------------------------------------------------
# BFS over the determinised product
# --------------------------------------------------------------------------

class Node:
    __slots__ = ("old_runs", "new_runs", "observables", "depth")

    def __init__(self, old_runs, new_runs, observables, depth):
        self.old_runs = old_runs
        self.new_runs = new_runs
        self.observables = observables  # tuple of (request, status, label)
        self.depth = depth


def _runs_key(runs, reg_names):
    return tuple(sorted(r.key(reg_names) for r in runs))


def _node_key(node, old_m: Machine, new_m: Machine):
    return (
        _runs_key(node.old_runs, old_m.reg_names),
        _runs_key(node.new_runs, new_m.reg_names),
    )


def _possibility(machine: Machine, pre_runs, obs_key, p_var):
    """Condition under which `machine` can emit observable `obs_key` next."""
    disjuncts = []
    for run in pre_runs:
        for t in machine.obs_from.get(run.state, {}).get(obs_key, []):
            disjuncts.append(
                z3.And(run.constraint, enabled_z3(t, run.regs, p_var))
            )
    return z3.Or(*disjuncts)


def _lexmin_params(cond, p_vars):
    """Lexicographically minimise p_vars subject to cond."""
    opt = z3.Optimize()
    opt.set(priority="lex")
    opt.set("timeout", SOLVER_TIMEOUT_MS * 2)
    opt.add(cond)
    for pv in p_vars:
        opt.minimize(pv)
    if opt.check() != z3.sat:
        raise EngineError("solver returned 'unknown' during minimisation")
    m = opt.model()
    return [m.eval(pv).as_long() for pv in p_vars]


def compare_models(old_model: StateModel, new_model: StateModel, max_requests: int):
    """Returns a result dict (see main.py for the wire format)."""
    started = time.monotonic()
    old_m, new_m = Machine(old_model), Machine(new_model)
    feas = Feasibility()
    p_vars = [z3.Int(f"p_{i}") for i in range(max_requests)]

    root = Node([old_m.initial_run()], [new_m.initial_run()], (), 0)
    level = [root]
    seen = {_node_key(root, old_m, new_m)}
    total_nodes = 1

    for depth in range(max_requests):
        if time.monotonic() - started > TIME_BUDGET_SECONDS:
            raise EngineError("analysis time budget exceeded")
        p_var = p_vars[depth]
        p_bounds = z3.And(p_var >= 0, p_var <= U32_MAX)

        # ---- pass 1: divergence check for every node at this level --------
        divergences = []  # (node, obs_key, condition, pre_old)
        prepared = {}  # id(node) -> (pre_old, pre_new, old_poss_by_key)
        for node in level:
            pre_old = tau_closure(old_m, node.old_runs, feas)
            pre_new = tau_closure(new_m, node.new_runs, feas)
            poss = {}
            for key in old_m.observables:
                old_poss = z3.And(
                    _possibility(old_m, pre_old, key, p_var), p_bounds
                )
                if not feas.sat(old_poss):
                    continue  # old cannot emit this observable here
                new_poss = z3.And(
                    _possibility(new_m, pre_new, key, p_var), p_bounds
                )
                diff = z3.And(old_poss, z3.Not(new_poss))
                if feas.sat(diff):
                    divergences.append((node, key, diff, pre_old))
                poss[key] = old_poss
            prepared[id(node)] = (pre_old, pre_new, poss)

        if divergences:
            return _build_counterexample(
                divergences, p_vars, old_m, new_m, max_requests
            )

        # ---- pass 2: expand children --------------------------------------
        if depth == max_requests - 1:
            break
        next_level = []
        next_by_key = {}
        for node in level:
            pre_old, pre_new, poss = prepared[id(node)]
            for key in sorted(poss):
                child = Node(
                    old_runs=fire(old_m, pre_old, key, p_var, feas),
                    new_runs=fire(new_m, pre_new, key, p_var, feas),
                    observables=node.observables + (key,),
                    depth=depth + 1,
                )
                k = _node_key(child, old_m, new_m)
                if k in seen:
                    continue
                # Same configuration sets reached via different prefixes: the
                # constraints are identical, so divergence conditions and
                # minimised parameters coincide; keep the lexicographically
                # smallest observable prefix for the final tie-break.
                existing = next_by_key.get(k)
                if existing is not None:
                    if child.observables < existing.observables:
                        idx = next_level.index(existing)
                        next_level[idx] = child
                        next_by_key[k] = child
                    continue
                seen.add(k)
                next_by_key[k] = child
                next_level.append(child)
        total_nodes += len(next_level)
        if total_nodes > MAX_NODES:
            raise EngineError("too many product nodes (state-space explosion)")
        level = next_level
        if not level:
            break

    return {
        "compatible": True,
        "checked_requests": max_requests,
        "detail": (
            f"Every request/response trace of the old model with up to "
            f"{max_requests} request(s) is reproducible by the new model. "
            f"This is a bounded result only; behaviour beyond {max_requests} "
            f"requests was not checked."
        ),
    }


# --------------------------------------------------------------------------
# Counterexample reconstruction
# --------------------------------------------------------------------------

def _build_counterexample(divergences, p_vars, old_m, new_m, max_requests):
    best = None  # (trace_tuple, node, obs_key, params, pre_old)
    for node, key, diff, pre_old in divergences:
        depth = node.depth
        vals = _lexmin_params(diff, p_vars[: depth + 1])
        trace = tuple(
            (obs[0], vals[i], obs[1], obs[2])
            for i, obs in enumerate(node.observables)
        ) + ((key[0], vals[depth], key[1], key[2]),)
        if best is None or trace < best[0]:
            best = (trace, node, key, vals, pre_old)
    trace, node, key, vals, pre_old = best

    # Find a concrete old run + final transition realising the trace.
    p_var = p_vars[node.depth]
    subst = [(p_vars[i], z3.IntVal(v)) for i, v in enumerate(vals)]
    witness = None
    for run in pre_old:
        for t in old_m.obs_from.get(run.state, {}).get(key, []):
            cond = z3.simplify(
                z3.substitute(
                    z3.And(run.constraint, enabled_z3(t, run.regs, p_var)), *subst
                )
            )
            if z3.is_true(cond):
                witness = (run, t)
                break
        if witness:
            break
    if witness is None:  # pragma: no cover - should be unreachable
        raise EngineError("internal error: no concrete old witness found")

    run, final_t = witness
    old_path = _replay_old_path(old_m, run.history + (final_t.id,), vals)
    new_reachable = _new_reachable_sets(new_m, trace)

    return {
        "compatible": False,
        "counterexample": {
            "length": len(trace),
            "trace": [
                {"request": r, "param": p, "status": s, "label": l}
                for (r, p, s, l) in trace
            ],
            "old_path": old_path,
            "new_reachable": new_reachable,
        },
    }


def _replay_old_path(machine: Machine, tids, params):
    """Concrete step-by-step execution of the chosen old run."""
    state = machine.model.initial_state
    regs = dict(machine.init_regs)
    events = []
    pi = 0
    for tid in tids:
        t = machine.by_id[tid]
        ev = {
            "kind": "tau" if t.is_tau else "request",
            "id": t.id,
            "from": state,
            "to": t.to_state,
        }
        param = None
        if not t.is_tau:
            param = params[pi]
            pi += 1
            ev.update(
                request=t.request,
                param=param,
                status=t.response.status,
                label=t.response.label,
            )
        new_regs = eval_update(t, regs, param)
        assert new_regs is not None  # enabledness was proven symbolically
        regs = new_regs
        state = t.to_state
        ev["registers"] = dict(regs)
        events.append(ev)
    return events


def _new_reachable_sets(machine: Machine, trace):
    """Reachable new configurations after each prefix of the trace.

    Index i holds the configurations reachable after the first i requests
    (0 = initial configuration).  The last entry is empty: that is exactly
    where the new model fails to reproduce the trace.
    """
    init = (machine.model.initial_state, tuple(machine.init_regs[r] for r in machine.reg_names))
    current = {init}
    snapshots = [_configs_json(machine, current)]
    for (req, param, status, label) in trace:
        # tau closure, at most two steps
        pre = set(current)
        frontier = set(current)
        for _ in range(2):
            nxt = set()
            for (st, rv) in frontier:
                regs = dict(zip(machine.reg_names, rv))
                for t in machine.taus_from.get(st, []):
                    if not eval_guard(t, regs, 0):
                        continue
                    new_regs = eval_update(t, regs, 0)
                    if new_regs is None:
                        continue
                    nxt.add((t.to_state, tuple(new_regs[r] for r in machine.reg_names)))
            pre |= nxt
            frontier = nxt
        nxt_set = set()
        for (st, rv) in pre:
            regs = dict(zip(machine.reg_names, rv))
            for t in machine.obs_from.get(st, {}).get((req, status, label), []):
                if not eval_guard(t, regs, param):
                    continue
                new_regs = eval_update(t, regs, param)
                if new_regs is None:
                    continue
                nxt_set.add(
                    (t.to_state, tuple(new_regs[r] for r in machine.reg_names))
                )
        current = nxt_set
        snapshots.append(_configs_json(machine, current))
    return snapshots


def _configs_json(machine: Machine, configs):
    return [
        {
            "state": st,
            "registers": dict(zip(machine.reg_names, rv)),
        }
        for (st, rv) in sorted(configs)
    ]
