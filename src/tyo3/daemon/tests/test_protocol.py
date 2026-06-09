"""Protocol (de)serialisation tests — pure, no session required."""

from __future__ import annotations

import json

import pytest

from tyo3.daemon import protocol as P


def test_parse_request_round_trip():
    line = json.dumps({"jsonrpc": "2.0", "id": 7, "method": "open", "params": {"root": "/tmp/x"}})
    req = P.parse_request(line)
    assert req.method == "open"
    assert req.id == 7
    assert req.params == {"root": "/tmp/x"}
    assert not req.is_notification


def test_parse_notification_has_no_id():
    req = P.parse_request(json.dumps({"jsonrpc": "2.0", "method": "ping"}))
    assert req.is_notification
    assert req.params == {}


def test_parse_rejects_non_object():
    with pytest.raises(P.ProtocolError) as e:
        P.parse_request("[1, 2, 3]")
    assert e.value.code == P.INVALID_REQUEST


def test_parse_rejects_bad_json():
    with pytest.raises(P.ProtocolError) as e:
        P.parse_request("{not json")
    assert e.value.code == P.PARSE_ERROR


def test_parse_rejects_missing_method():
    with pytest.raises(P.ProtocolError) as e:
        P.parse_request(json.dumps({"jsonrpc": "2.0", "id": 1}))
    assert e.value.code == P.INVALID_REQUEST
    assert e.value.request_id == 1


def test_parse_rejects_non_object_params():
    with pytest.raises(P.ProtocolError) as e:
        P.parse_request(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "m", "params": 5}))
    assert e.value.code == P.INVALID_PARAMS


def test_encode_response_is_newline_terminated():
    s = P.encode_response(3, {"ok": True})
    assert s.endswith("\n")
    obj = json.loads(s)
    assert obj == {"jsonrpc": "2.0", "id": 3, "result": {"ok": True}}


def test_encode_error_carries_code_and_optional_data():
    s = P.encode_error(4, P.ENGINE_ERROR, "boom", data={"hint": "x"})
    obj = json.loads(s)
    assert obj["error"]["code"] == P.ENGINE_ERROR
    assert obj["error"]["message"] == "boom"
    assert obj["error"]["data"] == {"hint": "x"}
    assert obj["id"] == 4


def test_encode_notification_has_no_id():
    s = P.encode_notification("delta", {"revision": 9})
    obj = json.loads(s)
    assert "id" not in obj
    assert obj["method"] == "delta"
    assert obj["params"] == {"revision": 9}
