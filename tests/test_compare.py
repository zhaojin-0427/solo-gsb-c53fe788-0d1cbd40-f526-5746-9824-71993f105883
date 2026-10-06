import copy

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

U32_MAX = 2**32 - 1


def threshold_model(threshold, status=200, label="ok"):
    """One request 'buy', enabled iff param <= threshold."""
    return {
        "initial_state": "s0",
        "registers": {},
        "transitions": [
            {
                "from": "s0",
                "to": "s0",
                "request": "buy",
                "guard": [{"terms": [{"param": True}], "op": "<=", "const": threshold}],
                "response": {"status": status, "label": label},
            }
        ],
    }


def test_identical_models_are_compatible():
    m = threshold_model(100)
    r = client.post("/compare", json={"old": m, "new": m})
    assert r.status_code == 200
    body = r.json()
    assert body["compatible"] is True
    assert body["checked_requests"] == 8
    assert "counterexample" not in body


def test_threshold_divergence_shortest_and_lex_min():
    # old accepts param <= 10, new only param <= 5 -> smallest witness is 6.
    r = client.post(
        "/compare", json={"old": threshold_model(10), "new": threshold_model(5)}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["compatible"] is False
    ce = body["counterexample"]
    assert ce["length"] == 1
    assert ce["trace"] == [
        {"request": "buy", "param": 6, "status": 200, "label": "ok"}
    ]
    # new model cannot reproduce anything after the failing request
    assert ce["new_reachable"][-1] == []
    assert ce["new_reachable"][0] == [{"state": "s0", "registers": {}}]
    assert ce["old_path"][0]["request"] == "buy"


def test_different_responses_to_same_request_must_match():
    old = {
        "initial_state": "s0",
        "registers": {},
        "transitions": [
            {"from": "s0", "to": "s0", "request": "pay",
             "response": {"status": 200, "label": "ok"}},
            {"from": "s0", "to": "s0", "request": "pay",
             "response": {"status": 409, "label": "duplicate"}},
        ],
    }
    new = copy.deepcopy(old)
    del new["transitions"][1]  # new only answers 200
    r = client.post("/compare", json={"old": old, "new": new})
    body = r.json()
    assert body["compatible"] is False
    assert body["counterexample"]["trace"] == [
        {"request": "pay", "param": 0, "status": 409, "label": "duplicate"}
    ]


def test_tau_steps_are_unobservable():
    # old silently initialises x via tau before serving; new serves directly.
    old = {
        "initial_state": "s0",
        "registers": {"x": 0},
        "transitions": [
            {"from": "s0", "to": "s1", "update": {"x": {"kind": "const", "value": 1}}},
            {"from": "s1", "to": "s1", "request": "ping",
             "guard": [{"terms": [{"reg": "x"}], "op": "==", "const": 1}],
             "response": {"status": 200, "label": "pong"}},
        ],
    }
    new = {
        "initial_state": "s0",
        "registers": {},
        "transitions": [
            {"from": "s0", "to": "s0", "request": "ping",
             "response": {"status": 200, "label": "pong"}}
        ],
    }
    r = client.post("/compare", json={"old": old, "new": new})
    assert r.json()["compatible"] is True


def test_out_of_bounds_update_cannot_be_taken():
    # old may increment twice (x < 2), new only once (x < 1).
    def counter(bound):
        return {
            "initial_state": "s0",
            "registers": {"x": 0},
            "transitions": [
                {"from": "s0", "to": "s0", "request": "inc",
                 "guard": [{"terms": [{"reg": "x"}], "op": "<", "const": bound}],
                 "update": {"x": {"kind": "add", "value": 1}},
                 "response": {"status": 200, "label": "ok"}}
            ],
        }

    r = client.post("/compare", json={"old": counter(2), "new": counter(1)})
    body = r.json()
    assert body["compatible"] is False
    assert body["counterexample"]["length"] == 2
    assert [e["request"] for e in body["counterexample"]["old_path"]] == [
        "inc",
        "inc",
    ]
    # and the other inclusion direction holds
    r2 = client.post("/compare", json={"old": counter(1), "new": counter(2)})
    assert r2.json()["compatible"] is True


def test_overflow_blocks_transition():
    # x starts at max; increment would overflow, so 'inc' is never enabled.
    old = {
        "initial_state": "s0",
        "registers": {"x": U32_MAX},
        "transitions": [
            {"from": "s0", "to": "s0", "request": "inc",
             "update": {"x": {"kind": "add", "value": 1}},
             "response": {"status": 200, "label": "ok"}}
        ],
    }
    new = {"initial_state": "s0", "registers": {}, "transitions": []}
    r = client.post("/compare", json={"old": old, "new": new})
    assert r.json()["compatible"] is True


def test_parameter_carried_across_requests():
    # old: set stores param, check requires equality; new requires strict <.
    def model(cmp_op):
        return {
            "initial_state": "s0",
            "registers": {"x": 0},
            "transitions": [
                {"from": "s0", "to": "s1", "request": "set",
                 "update": {"x": {"kind": "param"}},
                 "response": {"status": 200, "label": "ok"}},
                {"from": "s1", "to": "s1", "request": "check",
                 "guard": [
                     {"terms": [{"reg": "x"}, {"param": True, "coef": -1}],
                      "op": cmp_op, "const": 0}
                 ],
                 "response": {"status": 200, "label": "ok"}},
            ],
        }

    r = client.post("/compare", json={"old": model("=="), "new": model("<")})
    body = r.json()
    assert body["compatible"] is False
    assert body["counterexample"]["trace"] == [
        {"request": "set", "param": 0, "status": 200, "label": "ok"},
        {"request": "check", "param": 0, "status": 200, "label": "ok"},
    ]


def test_invalid_models_return_422():
    valid = threshold_model(1)

    three_regs = copy.deepcopy(valid)
    three_regs["registers"] = {"a": 0, "b": 0, "c": 0}
    assert client.post("/compare", json={"old": three_regs, "new": valid}).status_code == 422

    tau_with_param = {
        "initial_state": "s0",
        "registers": {"x": 0},
        "transitions": [
            {"from": "s0", "to": "s0",
             "guard": [{"terms": [{"param": True}], "op": ">", "const": 1}]}
        ],
    }
    assert client.post("/compare", json={"old": tau_with_param, "new": valid}).status_code == 422

    request_without_response = {
        "initial_state": "s0",
        "registers": {},
        "transitions": [{"from": "s0", "to": "s0", "request": "a"}],
    }
    assert client.post(
        "/compare", json={"old": request_without_response, "new": valid}
    ).status_code == 422

    unknown_register = copy.deepcopy(valid)
    unknown_register["transitions"][0]["guard"] = [
        {"terms": [{"reg": "nope"}], "op": "==", "const": 0}
    ]
    assert client.post(
        "/compare", json={"old": unknown_register, "new": valid}
    ).status_code == 422

    bad_initial = copy.deepcopy(valid)
    bad_initial["registers"] = {"x": U32_MAX + 1}
    assert client.post("/compare", json={"old": bad_initial, "new": valid}).status_code == 422


def test_single_threshold_model_is_fast():
    import time

    old = threshold_model(U32_MAX - 1)
    new = threshold_model(U32_MAX)
    start = time.monotonic()
    r = client.post("/compare", json={"old": old, "new": new})
    elapsed = time.monotonic() - start
    assert r.json()["compatible"] is True
    assert elapsed < 10


def test_at_most_two_tau_steps_before_a_request():
    # new needs a chain of 3 tau steps before it can answer; old answers at once.
    old = {
        "initial_state": "s0",
        "registers": {},
        "transitions": [
            {"from": "s0", "to": "s0", "request": "ping",
             "response": {"status": 200, "label": "pong"}}
        ],
    }
    new = {
        "initial_state": "n0",
        "registers": {},
        "transitions": [
            {"from": "n0", "to": "n1"},
            {"from": "n1", "to": "n2"},
            {"from": "n2", "to": "n3"},
            {"from": "n3", "to": "n3", "request": "ping",
             "response": {"status": 200, "label": "pong"}},
        ],
    }
    r = client.post("/compare", json={"old": old, "new": new})
    body = r.json()
    assert body["compatible"] is False
    assert body["counterexample"]["length"] == 1
    # with only 2 tau steps the chain is fine
    new["transitions"] = new["transitions"][1:]
    new["initial_state"] = "n1"
    r2 = client.post("/compare", json={"old": old, "new": new})
    assert r2.json()["compatible"] is True


def test_lexicographic_tie_break_by_request_name_and_param():
    # old can emit 'b' (any param) and 'a' (param >= 100); new can emit neither.
    old = {
        "initial_state": "s0",
        "registers": {},
        "transitions": [
            {"from": "s0", "to": "s0", "request": "b",
             "response": {"status": 200, "label": "ok"}},
            {"from": "s0", "to": "s0", "request": "a",
             "guard": [{"terms": [{"param": True}], "op": ">=", "const": 100}],
             "response": {"status": 200, "label": "ok"}},
        ],
    }
    new = {"initial_state": "s0", "registers": {}, "transitions": []}
    r = client.post("/compare", json={"old": old, "new": new})
    ce = r.json()["counterexample"]
    # 'a' < 'b', and the parameter is minimised within the guard (>= 100)
    assert ce["trace"] == [{"request": "a", "param": 100, "status": 200, "label": "ok"}]


def test_both_models_with_two_responses_are_compatible():
    m = {
        "initial_state": "s0",
        "registers": {},
        "transitions": [
            {"from": "s0", "to": "s0", "request": "pay",
             "response": {"status": 200, "label": "ok"}},
            {"from": "s0", "to": "s0", "request": "pay",
             "response": {"status": 409, "label": "duplicate"}},
        ],
    }
    r = client.post("/compare", json={"old": m, "new": copy.deepcopy(m)})
    assert r.json()["compatible"] is True


def test_nondeterministic_new_model_subset_tracking():
    # new must keep both branches of 'a' in its reachable set: only the n2
    # branch can serve 'b' afterwards, and that is enough for inclusion.
    old = {
        "initial_state": "s0",
        "registers": {},
        "transitions": [
            {"from": "s0", "to": "s1", "request": "a",
             "response": {"status": 200, "label": "ok"}},
            {"from": "s1", "to": "s1", "request": "b",
             "response": {"status": 200, "label": "ok"}},
        ],
    }
    new = {
        "initial_state": "n0",
        "registers": {},
        "transitions": [
            {"from": "n0", "to": "n1", "request": "a",
             "response": {"status": 200, "label": "ok"}},
            {"from": "n0", "to": "n2", "request": "a",
             "response": {"status": 200, "label": "ok"}},
            {"from": "n2", "to": "n2", "request": "b",
             "response": {"status": 200, "label": "ok"}},
        ],
    }
    r = client.post("/compare", json={"old": old, "new": new})
    assert r.json()["compatible"] is True
    # if the n2 branch is removed, 'a,b' can no longer be reproduced
    new["transitions"] = new["transitions"][::2]
    r2 = client.post("/compare", json={"old": old, "new": new})
    body = r2.json()
    assert body["compatible"] is False
    assert [e["request"] for e in body["counterexample"]["trace"]] == ["a", "b"]


def test_deep_bounded_chain():
    # a chain of exactly 8 increments is reproducible, the 9th is out of scope
    def chain(bound):
        return {
            "initial_state": "s0",
            "registers": {"x": 0},
            "transitions": [
                {"from": "s0", "to": "s0", "request": "tick",
                 "guard": [{"terms": [{"reg": "x"}], "op": "<", "const": bound}],
                 "update": {"x": {"kind": "add", "value": 1}},
                 "response": {"status": 200, "label": "ok"}}
            ],
        }

    r = client.post("/compare", json={"old": chain(8), "new": chain(8)})
    assert r.json()["compatible"] is True
    r2 = client.post("/compare", json={"old": chain(9), "new": chain(8)})
    body = r2.json()
    # the divergence needs 9 requests, beyond the checked bound of 8
    assert body["compatible"] is True
    assert body["checked_requests"] == 8
