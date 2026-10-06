"""End-to-end tests for the compatibility checker API."""
import time

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

MAX32 = 2**32 - 1


def compare(body):
    return client.post("/compare", json=body)


# ---------------------------------------------------------------------------
# models used by several tests
# ---------------------------------------------------------------------------

def threshold_model(threshold, ok_label="ok", big_label="big"):
    """Single-threshold 32-bit parameter model: 'check' answers ok/big."""
    return {
        "initial_state": "s",
        "registers": {},
        "transitions": [
            {
                "id": "small",
                "from": "s",
                "to": "s",
                "request": "check",
                "guard": [{"terms": [{"param": True}], "op": "<=", "const": threshold}],
                "response": {"status": 200, "label": ok_label},
            },
            {
                "id": "big",
                "from": "s",
                "to": "s",
                "request": "check",
                "guard": [{"terms": [{"param": True}], "op": ">", "const": threshold}],
                "response": {"status": 200, "label": big_label},
            },
        ],
    }


# ---------------------------------------------------------------------------
# basic outcomes
# ---------------------------------------------------------------------------

def test_identical_models_are_compatible():
    body = {"old": threshold_model(1000), "new": threshold_model(1000)}
    resp = compare(body)
    assert resp.status_code == 200
    data = resp.json()
    assert data["result"] == "compatible"
    assert data["checked_requests"] == 8
    assert "counterexample" not in data


def test_threshold_change_is_incompatible_with_shortest_counterexample():
    body = {"old": threshold_model(1000), "new": threshold_model(999)}
    resp = compare(body)
    assert resp.status_code == 200
    data = resp.json()
    assert data["result"] == "incompatible"
    cex = data["counterexample"]
    assert cex["length"] == 1
    # the single diverging point: param 1000 is "ok" in the old, "big" in the new
    assert cex["trace"] == [
        {"request": "check", "param": 1000, "status": 200, "label": "ok"}
    ]
    # old path and new step-by-step reachability are reported
    assert data["old_path"]["initial"] == {"state": "s", "registers": {}}
    assert data["old_path"]["steps"][0]["request"] == "check"
    reach = data["new_reachable"]
    assert len(reach) == 1
    assert reach[0]["response_reproduced"] is False
    assert reach[0]["new_possible_responses"] == [{"status": 200, "label": "big"}]


def test_new_may_do_more_than_old():
    # new answers "ok" for every parameter: it reproduces all old traces.
    old = threshold_model(1000)
    new = {
        "initial_state": "s",
        "registers": {},
        "transitions": [
            {
                "from": "s", "to": "s", "request": "check",
                "response": {"status": 200, "label": "ok"},
            },
            {
                "from": "s", "to": "s", "request": "check",
                "response": {"status": 200, "label": "big"},
            },
            {
                "from": "s", "to": "s", "request": "extra",
                "response": {"status": 418, "label": "teapot"},
            },
        ],
    }
    resp = compare({"old": old, "new": new})
    assert resp.status_code == 200
    assert resp.json()["result"] == "compatible"


def test_same_request_different_responses_must_all_be_reproduced():
    old = {
        "initial_state": "s",
        "registers": {},
        "transitions": [
            {"from": "s", "to": "s", "request": "get",
             "response": {"status": 200, "label": "cached"}},
            {"from": "s", "to": "s", "request": "get",
             "response": {"status": 200, "label": "fresh"}},
        ],
    }
    new = {
        "initial_state": "s",
        "registers": {},
        "transitions": [
            {"from": "s", "to": "s", "request": "get",
             "response": {"status": 200, "label": "fresh"}},
        ],
    }
    data = compare({"old": old, "new": new}).json()
    assert data["result"] == "incompatible"
    # "cached" < "fresh" and only "cached" is missing
    assert data["counterexample"]["trace"] == [
        {"request": "get", "param": 0, "status": 200, "label": "cached"}
    ]


# ---------------------------------------------------------------------------
# registers, guards, tau steps, bounds
# ---------------------------------------------------------------------------

def test_lexicographic_tie_break_minimizes_parameter():
    old = {
        "initial_state": "s",
        "registers": {},
        "transitions": [
            {"from": "s", "to": "s", "request": "buy",
             "guard": [{"terms": [{"param": True}], "op": ">=", "const": 5}],
             "response": {"status": 200, "label": "ok"}},
        ],
    }
    new = {
        "initial_state": "s",
        "registers": {},
        "transitions": [
            {"from": "s", "to": "s", "request": "buy",
             "guard": [{"terms": [{"param": True}], "op": ">=", "const": 10}],
             "response": {"status": 200, "label": "ok"}},
        ],
    }
    data = compare({"old": old, "new": new}).json()
    assert data["result"] == "incompatible"
    assert data["counterexample"]["trace"] == [
        {"request": "buy", "param": 5, "status": 200, "label": "ok"}
    ]


def test_tau_steps_are_part_of_the_semantics():
    # old can increment x silently once, then serve "get" only when x == 1.
    old = {
        "initial_state": "s",
        "registers": {"x": 0},
        "transitions": [
            {"id": "tick", "from": "s", "to": "s",
             "guard": [{"terms": [{"reg": "x"}], "op": "<", "const": 1}],
             "update": {"x": {"add": 1}}},
            {"id": "serve", "from": "s", "to": "s", "request": "get",
             "guard": [{"terms": [{"reg": "x"}], "op": "==", "const": 1}],
             "response": {"status": 200, "label": "ok"}},
        ],
    }
    # new without the tau step can never serve "get".
    new = {
        "initial_state": "s",
        "registers": {"x": 0},
        "transitions": [
            {"id": "serve", "from": "s", "to": "s", "request": "get",
             "guard": [{"terms": [{"reg": "x"}], "op": "==", "const": 1}],
             "response": {"status": 200, "label": "ok"}},
        ],
    }
    data = compare({"old": old, "new": new}).json()
    assert data["result"] == "incompatible"
    assert data["counterexample"]["trace"] == [
        {"request": "get", "param": 0, "status": 200, "label": "ok"}
    ]
    kinds = [step["kind"] for step in data["old_path"]["steps"]]
    assert kinds == ["tau", "request"]
    # new is stuck in the initial state
    assert data["new_reachable"][0]["states_after_request"] == []

    # a new model with the same tau step is compatible again
    data = compare({"old": old, "new": old}).json()
    assert data["result"] == "compatible"


def test_out_of_bounds_update_cannot_be_taken():
    # "bump" at x == MAX32-1 succeeds once; a second bump would overflow, so
    # the old model has no two-request trace.
    old = {
        "initial_state": "s",
        "registers": {"x": MAX32 - 1},
        "transitions": [
            {"from": "s", "to": "s", "request": "bump",
             "update": {"x": {"add": 1}},
             "response": {"status": 200, "label": "ok"}},
        ],
    }
    assert compare({"old": old, "new": old}).json()["result"] == "compatible"

    # new starts at MAX32 and can never bump: the single old trace is lost.
    new = {
        "initial_state": "s",
        "registers": {"x": MAX32},
        "transitions": [
            {"from": "s", "to": "s", "request": "bump",
             "update": {"x": {"add": 1}},
             "response": {"status": 200, "label": "ok"}},
        ],
    }
    data = compare({"old": old, "new": new}).json()
    assert data["result"] == "incompatible"
    assert data["counterexample"]["length"] == 1


def test_register_linear_guards_with_two_registers():
    old = {
        "initial_state": "s",
        "registers": {"x": 3, "y": 1},
        "transitions": [
            {"from": "s", "to": "s", "request": "use",
             "guard": [
                 {"terms": [{"reg": "x"}, {"reg": "y", "coef": 2}], "op": ">=", "const": 5},
                 {"terms": [{"reg": "x"}, {"param": True, "coef": -1}], "op": "<=", "const": 0},
             ],
             "update": {"x": {"sub": 1}, "y": {"param": True}},
             "response": {"status": 200, "label": "ok"}},
        ],
    }
    assert compare({"old": old, "new": old}).json()["result"] == "compatible"


def test_bounded_check_declares_only_checked_bound():
    # divergence needs 4 requests (3 ticks, then "read" at x == 3)
    old = {
        "initial_state": "s",
        "registers": {"x": 0},
        "transitions": [
            {"from": "s", "to": "s", "request": "tick",
             "guard": [{"terms": [{"reg": "x"}], "op": "<", "const": 5}],
             "update": {"x": {"add": 1}},
             "response": {"status": 200, "label": "ticked"}},
            {"from": "s", "to": "s", "request": "read",
             "guard": [{"terms": [{"reg": "x"}], "op": "==", "const": 3}],
             "response": {"status": 200, "label": "three"}},
        ],
    }
    new = {
        "initial_state": "s",
        "registers": {"x": 0},
        "transitions": [
            {"from": "s", "to": "s", "request": "tick",
             "guard": [{"terms": [{"reg": "x"}], "op": "<", "const": 5}],
             "update": {"x": {"add": 1}},
             "response": {"status": 200, "label": "ticked"}},
            {"from": "s", "to": "s", "request": "read",
             "guard": [{"terms": [{"reg": "x"}], "op": "==", "const": 2}],
             "response": {"status": 200, "label": "three"}},
        ],
    }
    data = compare({"old": old, "new": new, "max_requests": 3}).json()
    assert data["result"] == "compatible"
    assert data["checked_requests"] == 3

    data = compare({"old": old, "new": new, "max_requests": 4}).json()
    assert data["result"] == "incompatible"
    assert data["counterexample"]["length"] == 4
    assert [step["request"] for step in data["counterexample"]["trace"]] == [
        "tick", "tick", "tick", "read",
    ]


def test_multi_step_counterexample_and_reachability_report():
    # new can only afford one "pay" (budget y), old can pay twice.
    old = {
        "initial_state": "s",
        "registers": {"y": 2},
        "transitions": [
            {"from": "s", "to": "s", "request": "pay",
             "guard": [{"terms": [{"reg": "y"}], "op": ">", "const": 0}],
             "update": {"y": {"sub": 1}},
             "response": {"status": 200, "label": "paid"}},
        ],
    }
    new = {
        "initial_state": "s",
        "registers": {"y": 1},
        "transitions": [
            {"from": "s", "to": "s", "request": "pay",
             "guard": [{"terms": [{"reg": "y"}], "op": ">", "const": 0}],
             "update": {"y": {"sub": 1}},
             "response": {"status": 200, "label": "paid"}},
        ],
    }
    data = compare({"old": old, "new": new}).json()
    assert data["result"] == "incompatible"
    assert data["counterexample"]["length"] == 2
    reach = data["new_reachable"]
    assert reach[0]["response_reproduced"] is True
    assert reach[1]["response_reproduced"] is False
    assert reach[1]["states_before_request"] == [
        {"state": "s", "registers": {"y": 0}}
    ]


# ---------------------------------------------------------------------------
# performance requirement: single-threshold 32-bit parameter models
# ---------------------------------------------------------------------------

def test_single_threshold_32bit_model_is_decided_quickly():
    old = threshold_model(2**31)
    new = threshold_model(2**31 - 1)
    start = time.monotonic()
    resp = compare({"old": old, "new": new})
    elapsed = time.monotonic() - start
    assert resp.status_code == 200
    assert resp.json()["result"] == "incompatible"
    assert resp.json()["counterexample"]["trace"][0]["param"] == 2**31
    assert elapsed < 10


def test_single_threshold_32bit_model_compatible_quickly():
    start = time.monotonic()
    resp = compare({"old": threshold_model(MAX32), "new": threshold_model(MAX32)})
    elapsed = time.monotonic() - start
    assert resp.json()["result"] == "compatible"
    assert elapsed < 10


# ---------------------------------------------------------------------------
# invalid models -> 422
# ---------------------------------------------------------------------------

VALID = threshold_model(1)


@pytest.mark.parametrize("broken", [
    pytest.param(lambda m: m.update(registers={"a": 0, "b": 0, "c": 0}), id="three-registers"),
    pytest.param(lambda m: m.update(registers={"x": -1}), id="negative-initial"),
    pytest.param(lambda m: m.update(registers={"x": MAX32 + 1}), id="initial-too-large"),
    pytest.param(lambda m: m["transitions"].append(
        {"from": "s", "to": "s",
         "guard": [{"terms": [{"param": True}], "op": ">", "const": 0}]}), id="tau-with-param-guard"),
    pytest.param(lambda m: m["transitions"].append(
        {"from": "s", "to": "s", "update": {"x": {"param": True}}}), id="tau-with-param-update"),
    pytest.param(lambda m: m["transitions"].append(
        {"from": "s", "to": "s", "response": {"status": 200, "label": "x"}}), id="tau-with-response"),
    pytest.param(lambda m: m["transitions"].append(
        {"from": "s", "to": "s", "request": "q"}), id="request-without-response"),
    pytest.param(lambda m: m["transitions"].append(
        {"from": "s", "to": "s", "request": "q",
         "guard": [{"terms": [{"reg": "nope"}], "op": "==", "const": 0}],
         "response": {"status": 200, "label": "x"}}), id="guard-unknown-register"),
    pytest.param(lambda m: m["transitions"].append(
        {"from": "s", "to": "s", "request": "q", "update": {"nope": {"set": 1}},
         "response": {"status": 200, "label": "x"}}), id="update-unknown-register"),
    pytest.param(lambda m: m["transitions"].append(
        {"from": "s", "to": "s", "request": "q",
         "guard": [{"terms": [{"param": True}], "op": "=<", "const": 0}],
         "response": {"status": 200, "label": "x"}}), id="bad-operator"),
    pytest.param(lambda m: m["transitions"].append(
        {"from": "s", "to": "s", "request": "q", "update": {"x": {"set": 1, "add": 1}},
         "response": {"status": 200, "label": "x"}}), id="ambiguous-update"),
    pytest.param(lambda m: m["transitions"].append(
        {"from": "s", "to": "s", "request": "q", "update": {"x": {"set": MAX32 + 1}},
         "response": {"status": 200, "label": "x"}}), id="set-out-of-range"),
    pytest.param(lambda m: m["transitions"].append(
        {"from": "s", "to": "s", "request": "q",
         "response": {"status": 99, "label": "x"}}), id="bad-status"),
])
def test_invalid_old_model_is_422(broken):
    import copy
    model = copy.deepcopy(VALID)
    model["registers"] = {"x": 0}
    broken(model)
    resp = compare({"old": model, "new": VALID})
    assert resp.status_code == 422


def test_invalid_new_model_is_422():
    new = threshold_model(1)
    new["registers"] = {"x": 0, "y": 0, "z": 0}
    assert compare({"old": VALID, "new": new}).status_code == 422


@pytest.mark.parametrize("value", [0, 9, -1, "eight", 1.5])
def test_invalid_max_requests_is_422(value):
    assert compare({"old": VALID, "new": VALID, "max_requests": value}).status_code == 422


def test_missing_sections_are_422():
    assert client.post("/compare", json={"old": VALID}).status_code == 422
    assert client.post("/compare", json={}).status_code == 422


def test_healthz():
    assert client.get("/healthz").json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# additional edge cases
# ---------------------------------------------------------------------------

def test_parameter_flows_through_register_across_cycles():
    # old stores the parameter, then compares against 5; new compares against 4.
    old = {
        "initial_state": "s",
        "registers": {"x": 0},
        "transitions": [
            {"from": "s", "to": "s", "request": "set",
             "update": {"x": {"param": True}},
             "response": {"status": 200, "label": "stored"}},
            {"from": "s", "to": "s", "request": "check",
             "guard": [{"terms": [{"reg": "x"}], "op": "<=", "const": 5}],
             "response": {"status": 200, "label": "ok"}},
        ],
    }
    new = {
        "initial_state": "s",
        "registers": {"x": 0},
        "transitions": [
            {"from": "s", "to": "s", "request": "set",
             "update": {"x": {"param": True}},
             "response": {"status": 200, "label": "stored"}},
            {"from": "s", "to": "s", "request": "check",
             "guard": [{"terms": [{"reg": "x"}], "op": "<=", "const": 4}],
             "response": {"status": 200, "label": "ok"}},
        ],
    }
    data = compare({"old": old, "new": new}).json()
    assert data["result"] == "incompatible"
    assert data["counterexample"]["trace"] == [
        {"request": "set", "param": 5, "status": 200, "label": "stored"},
        {"request": "check", "param": 0, "status": 200, "label": "ok"},
    ]


def test_empty_old_model_is_vacuously_compatible():
    old = {"initial_state": "s", "registers": {}, "transitions": []}
    data = compare({"old": old, "new": threshold_model(1)}).json()
    assert data["result"] == "compatible"


def test_empty_new_model_cannot_reproduce_anything():
    new = {"initial_state": "s", "registers": {}, "transitions": []}
    data = compare({"old": threshold_model(1), "new": new}).json()
    assert data["result"] == "incompatible"
    assert data["counterexample"]["length"] == 1
    assert data["new_reachable"][0]["states_after_request"] == []


def test_lexicographic_order_prefers_smaller_request_name():
    # both "a" and "b" diverge; the counterexample must start with "a".
    def one(name):
        return {"from": "s", "to": "s", "request": name,
                "response": {"status": 200, "label": "ok"}}
    old = {"initial_state": "s", "registers": {}, "transitions": [one("a"), one("b")]}
    new = {"initial_state": "s", "registers": {}, "transitions": []}
    data = compare({"old": old, "new": new}).json()
    assert data["counterexample"]["trace"][0]["request"] == "a"


def test_new_tau_may_compensate_missing_guard():
    # old serves "get" when x == 0; new increments x silently but can still
    # serve x == 0, so all old traces survive.
    old = {
        "initial_state": "s",
        "registers": {"x": 0},
        "transitions": [
            {"from": "s", "to": "s", "request": "get",
             "guard": [{"terms": [{"reg": "x"}], "op": "==", "const": 0}],
             "response": {"status": 200, "label": "ok"}},
        ],
    }
    new = {
        "initial_state": "s",
        "registers": {"x": 0},
        "transitions": [
            {"from": "s", "to": "s",
             "guard": [{"terms": [{"reg": "x"}], "op": "<", "const": 3}],
             "update": {"x": {"add": 1}}},
            {"from": "s", "to": "s", "request": "get",
             "guard": [{"terms": [{"reg": "x"}], "op": "==", "const": 0}],
             "response": {"status": 200, "label": "ok"}},
        ],
    }
    assert compare({"old": old, "new": new}).json()["result"] == "compatible"


def test_status_code_is_part_of_the_trace():
    old = {
        "initial_state": "s",
        "registers": {},
        "transitions": [
            {"from": "s", "to": "s", "request": "del",
             "response": {"status": 404, "label": "missing"}},
        ],
    }
    new = {
        "initial_state": "s",
        "registers": {},
        "transitions": [
            {"from": "s", "to": "s", "request": "del",
             "response": {"status": 410, "label": "missing"}},
        ],
    }
    data = compare({"old": old, "new": new}).json()
    assert data["result"] == "incompatible"
    assert data["counterexample"]["trace"] == [
        {"request": "del", "param": 0, "status": 404, "label": "missing"}
    ]
    assert data["new_reachable"][0]["new_possible_responses"] == [
        {"status": 410, "label": "missing"}
    ]


# ---------------------------------------------------------------------------
# regression: symbolic register offset must survive into guards
# ---------------------------------------------------------------------------

def test_regression_param_offset_in_later_guard():
    # x := param; x += 4; later guard x - param' > 2 must use x = param + 4.
    model = {
        "initial_state": "s0",
        "registers": {"x": 4, "y": 0},
        "transitions": [
            {"from": "s2", "to": "s0", "request": "r2",
             "guard": [{"terms": [{"reg": "x"}, {"param": True}], "op": "<=", "const": 7}],
             "update": {"x": {"add": 4}},
             "response": {"status": 201, "label": "ok"}},
            {"from": "s1", "to": "s2", "request": "r0",
             "update": {"x": {"param": True}, "y": {"param": True}},
             "response": {"status": 201, "label": "done"}},
            {"from": "s0", "to": "s1", "request": "r2",
             "guard": [{"terms": [{"reg": "x"}, {"param": True, "coef": -1}], "op": ">", "const": 2}],
             "response": {"status": 201, "label": "new"}},
        ],
    }
    assert compare({"old": model, "new": model}).json()["result"] == "compatible"


def test_seeded_fuzz_self_comparison_and_cross_check():
    """Deterministic fuzz: self-comparison is always compatible; the concrete
    reachability report agrees with the counterexample."""
    import random
    random.seed(7)

    def rand_model():
        states = ["s0", "s1", "s2"]
        regs = {"x": random.randint(0, 5), "y": random.randint(0, 3)}
        names = ["r0", "r1", "r2"]
        trans = []
        for _ in range(8):
            is_tau = random.random() < 0.3
            guard = []
            if random.random() < 0.7:
                terms = [{"reg": random.choice(list(regs))}]
                if not is_tau and random.random() < 0.5:
                    terms.append({"param": True, "coef": random.choice([-1, 1])})
                guard.append({"terms": terms,
                              "op": random.choice(["<=", ">=", "<", ">", "=="]),
                              "const": random.randint(0, 10)})
            update = {}
            for r in regs:
                if random.random() < 0.4:
                    kind = random.choice(["set", "add", "sub"] + ([] if is_tau else ["param"]))
                    update[r] = {kind: True} if kind == "param" else {kind: random.randint(0, 4)}
            tr = {"from": random.choice(states), "to": random.choice(states),
                  "guard": guard, "update": update}
            if not is_tau:
                tr["request"] = random.choice(names)
                tr["response"] = {"status": random.choice([200, 201, 404]),
                                  "label": random.choice(["ok", "done", "new"])}
            trans.append(tr)
        return {"initial_state": "s0", "registers": regs, "transitions": trans}

    for _ in range(6):
        model = rand_model()
        assert compare({"old": model, "new": model}).json()["result"] == "compatible"

    for _ in range(6):
        old, new = rand_model(), rand_model()
        data = compare({"old": old, "new": new}).json()
        if data["result"] == "incompatible":
            reach = data["new_reachable"]
            assert all(e["response_reproduced"] for e in reach[:-1])
            assert not reach[-1]["response_reproduced"]
