# Per-user keys (`ttp secret`): the endpoint/<alias> slot for OpenAI-compatible endpoints (endpoint.py).
# Run from tests/test_tt_project_runtime.py (see _include_cases there): helpers, fixtures and imports
# come from that file, and pytest collects these tests under it. No test reaches the network.
from __future__ import annotations

_EP_KEY = "sk-placeholder-0123456789"
_EP_URL = "https://api.example.com/v1"


def _endpoint_home(env, monkeypatch):
    from ttp import project
    monkeypatch.setattr(project, "HOME_DIR", env["tmp"] / "secrets-home")
    return project


def _secret(monkeypatch, *argv, key=_EP_KEY):
    from ttp import cli
    monkeypatch.setattr(sys, "stdin", io.StringIO(key + "\n"))
    cli.main(["secret", *argv])


def test_secret_endpoint_saves_key_and_url_after_a_passing_check(env, monkeypatch, capsys):
    from ttp import endpoint
    project = _endpoint_home(env, monkeypatch)
    seen = []
    monkeypatch.setattr(endpoint, "verify", lambda url, key, timeout=10: (seen.append((url, key)), (True, "HTTP 200"))[1])
    _secret(monkeypatch, "endpoint", "tt-cloud", "--url", _EP_URL + "/")
    out = capsys.readouterr()
    assert seen == [(_EP_URL, _EP_KEY)]
    assert "endpoint/tt-cloud" in out.out and _EP_KEY not in out.out + out.err
    saved = project.load_secrets()["endpoint/tt-cloud"]
    assert saved["key"] == _EP_KEY and saved["url"] == _EP_URL and saved["verified"] is True
    assert project.secrets_path().stat().st_mode & 0o777 == 0o600


def test_secret_endpoint_failed_check_refuses_to_save_unless_forced(env, monkeypatch, capsys):
    from ttp import endpoint
    project = _endpoint_home(env, monkeypatch)
    monkeypatch.setattr(endpoint, "verify", lambda url, key, timeout=10: (False, "HTTP 401 Unauthorized"))
    with pytest.raises(SystemExit) as e:
        _secret(monkeypatch, "endpoint", "tt-cloud", "--url", _EP_URL)
    assert e.value.code != 0
    out = capsys.readouterr()
    assert "HTTP 401" in out.err and "--force" in out.err and _EP_KEY not in out.out + out.err
    assert "endpoint/tt-cloud" not in project.load_secrets()
    _secret(monkeypatch, "endpoint", "tt-cloud", "--url", _EP_URL, "--force")
    saved = project.load_secrets()["endpoint/tt-cloud"]
    assert saved["key"] == _EP_KEY and saved["verified"] is False and saved["check"] == "HTTP 401 Unauthorized"
    assert _EP_KEY not in capsys.readouterr().out


def test_secret_show_lists_endpoints_with_url_and_state_never_the_key(env, monkeypatch, capsys):
    from ttp import endpoint
    project = _endpoint_home(env, monkeypatch)
    project.save_secret("jev", {"via": "typesafe", "key": "jev-key-placeholder"})
    endpoint.save("tt-cloud", _EP_URL, _EP_KEY, True, "HTTP 200")
    endpoint.save("other_2", "http://127.0.0.1:8000/v1", "other-key-placeholder", False, "HTTP 503")
    from ttp import cli
    cli.main(["secret", "show"])
    out = capsys.readouterr().out
    assert _EP_KEY not in out and "other-key-placeholder" not in out and "jev-key-placeholder" not in out
    lines = out.splitlines()
    assert json.loads(lines[0]) == {"jev": ["key", "via"]}   # the other slots keep their JSON line
    assert lines[1:] == ["endpoint/other_2 http://127.0.0.1:8000/v1 not verified (HTTP 503)",
                         f"endpoint/tt-cloud {_EP_URL} verified"]


def test_secret_endpoint_refuses_a_bad_alias_url_or_empty_key(env, monkeypatch, capsys):
    from ttp import endpoint
    project = _endpoint_home(env, monkeypatch)
    monkeypatch.setattr(endpoint, "verify", lambda url, key, timeout=10: pytest.fail("checked a refused key"))
    for argv in (["endpoint", "a/b", "--url", _EP_URL], ["endpoint", "../x", "--url", _EP_URL],
                 ["endpoint", "has space", "--url", _EP_URL], ["endpoint", "--url", _EP_URL],
                 ["endpoint", "ok", "--url", "api.example.com/v1"], ["endpoint", "ok"],
                 ["jev", "stray-alias"]):
        with pytest.raises(SystemExit) as e:
            _secret(monkeypatch, *argv)
        assert e.value.code != 0, argv
    with pytest.raises(SystemExit):
        _secret(monkeypatch, "endpoint", "ok", "--url", _EP_URL, key="")
    assert _EP_KEY not in capsys.readouterr().err
    assert not project.load_secrets()
    with pytest.raises(ValueError):
        endpoint.credentials("a/b")


def test_endpoint_credentials_return_the_saved_key_and_url(env, monkeypatch):
    from ttp import endpoint
    _endpoint_home(env, monkeypatch)
    assert endpoint.credentials("tt-cloud") is None
    monkeypatch.setattr(endpoint, "verify", lambda url, key, timeout=10: (True, "HTTP 200"))
    _secret(monkeypatch, "endpoint", "tt-cloud", "--url", _EP_URL)
    assert endpoint.credentials("tt-cloud") == (_EP_KEY, _EP_URL)
    assert endpoint.credentials("other") is None


def test_endpoint_verify_sends_a_bearer_get_to_models_and_keeps_the_key_out_of_errors(monkeypatch):
    from ttp import endpoint
    sent = []

    class Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def urlopen(req, timeout=None):
        sent.append((req.get_method(), req.full_url, req.get_header("Authorization"), timeout))
        if len(sent) == 1:
            return Resp()
        if len(sent) == 2:
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)
        if len(sent) == 3:
            raise urllib.error.URLError(f"refused for {_EP_KEY}")
        raise TimeoutError(f"timed out sending {_EP_KEY}")
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    assert endpoint.verify(_EP_URL, _EP_KEY) == (True, "HTTP 200")
    assert sent[0] == ("GET", f"{_EP_URL}/models", f"Bearer {_EP_KEY}", endpoint.VERIFY_TIMEOUT_S)
    assert endpoint.VERIFY_TIMEOUT_S <= 30
    for want in ("HTTP 401 Unauthorized", "cannot reach", "TimeoutError"):
        ok, msg = endpoint.verify(_EP_URL, _EP_KEY)
        assert not ok and want in msg and _EP_KEY not in msg, msg
