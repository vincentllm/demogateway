from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, asdict

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from pydantic import SecretStr

from ai_gateway.auth import (
    AuthConfig, AuthConfigurationError, AuthError, AuthManager, ClientConfig,
    ClientKeyConfig, Identity, UpstreamKeyConfig, UpstreamKeyUnavailable,
    UpstreamPoolConfig, hash_client_key,
)

ALICE = "client-alice-secret-0123456789"
ALICE_NEW = "client-alice-rotated-9876543210"
BOB = "client-bob-secret-0123456789"
REVOKED = "client-disabled-secret-0123456789"
UPSTREAM_1 = "provider-secret-one-0123456789"
UPSTREAM_2 = "provider-secret-two-0123456789"


def key(name, token, *, enabled=True):
    return ClientKeyConfig(key_id=name, sha256=hash_client_key(token), enabled=enabled)


def settings(*, alice_keys=None, alice_enabled=True, pools=None):
    return AuthConfig(
        clients=[
            ClientConfig(client_id="alice", keys=alice_keys if alice_keys is not None else [
                key("alice-old", ALICE), key("alice-disabled", REVOKED, enabled=False),
            ], allowed_models=["gpt-test"], enabled=alice_enabled),
            ClientConfig(client_id="bob", keys=[key("bob", BOB)], allowed_models=["claude-test"]),
        ],
        upstream_pools=pools if pools is not None else [
            UpstreamPoolConfig(key_ref="openai", keys=[
                UpstreamKeyConfig(key_id="p1", secret=UPSTREAM_1),
                UpstreamKeyConfig(key_id="off", secret="provider-disabled", enabled=False),
                UpstreamKeyConfig(key_id="p2", secret=SecretStr(UPSTREAM_2)),
            ])
        ],
    )


@pytest.fixture
def manager():
    return AuthManager(settings())


def test_hash_matches_external_sha256_oracle():
    # Fixed external SHA-256 vector; this expected digest is not produced by
    # the implementation under test.
    assert hash_client_key("abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert hash_client_key(ALICE) == hashlib.sha256(ALICE.encode()).hexdigest()


@pytest.mark.parametrize("header", [
    None, "", "Bearer", "Bearer ", "Basic "+ALICE, ALICE,
    "Bearer unknown", "Bearer "+REVOKED, "Bearer "+UPSTREAM_1,
    "Bearer\t"+ALICE, " Bearer "+ALICE, "Bearer "+ALICE+" ",
    "Bearer "+ALICE+", Bearer "+BOB, "Bearer "+ALICE+"\r\nX: injected",
    "Bearer \u2603", "Bearer "+"x"*5000, 17, ["Bearer "+ALICE],
])
def test_missing_wrong_revoked_malformed_are_identical_401(manager, header):
    with pytest.raises(AuthError) as caught:
        manager.authenticate(header, "gpt-test")
    response = caught.value.to_response()
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert json.loads(response.body) == {"error": {
        "message": "Invalid API key.", "type": "authentication_error",
        "param": None, "code": "invalid_api_key",
    }}
    assert ALICE not in str(caught.value) + repr(caught.value)


@pytest.mark.parametrize("scheme", ["Bearer", "bearer", "bEaReR", "Bearer  "])
def test_identity_and_case_insensitive_bearer_scheme(manager, scheme):
    identity = manager.authenticate(scheme+" "+ALICE, "gpt-test")
    assert identity == Identity(client_id="alice", allowed_models=frozenset({"gpt-test"}))
    assert asdict(identity) == {"client_id": "alice", "allowed_models": frozenset({"gpt-test"})}
    with pytest.raises(FrozenInstanceError):
        identity.client_id = "bob"


@pytest.mark.parametrize("model", ["claude-test", "gpt-test-extra", "", None, [], "gpt-test\n"])
def test_model_access_denial_is_403_after_authentication(manager, model):
    with pytest.raises(AuthError) as caught:
        manager.authenticate("Bearer "+ALICE, model)
    response = caught.value.to_response()
    assert response.status_code == 403
    assert "www-authenticate" not in response.headers
    assert json.loads(response.body)["error"]["code"] == "model_not_allowed"
    # Invalid credentials must not reveal any model authorization outcome.
    with pytest.raises(AuthError) as invalid:
        manager.authenticate("Bearer unknown", model)
    assert invalid.value.status_code == 401


def test_clients_are_isolated_and_disabled_client_is_401(manager):
    assert manager.authenticate("Bearer "+BOB, "claude-test").client_id == "bob"
    with pytest.raises(AuthError) as caught:
        manager.authenticate("Bearer "+BOB, "gpt-test")
    assert caught.value.status_code == 403
    manager.reload(settings(alice_enabled=False))
    with pytest.raises(AuthError) as caught:
        manager.authenticate("Bearer "+ALICE, "gpt-test")
    assert caught.value.status_code == 401
    assert manager.authenticate("Bearer "+BOB, "claude-test").client_id == "bob"


def test_default_deny_and_explicit_wildcard():
    manager = AuthManager(AuthConfig(clients=[ClientConfig(client_id="a", keys=[key("k", ALICE)])]))
    with pytest.raises(AuthError) as denied:
        manager.authenticate("Bearer "+ALICE, "anything")
    assert denied.value.status_code == 403
    manager.reload(AuthConfig(clients=[ClientConfig(client_id="a", keys=[key("k", ALICE)], allowed_models=["*"])]))
    assert manager.authenticate("Bearer "+ALICE, "anything").client_id == "a"


def test_client_rotation_overlap_revocation_and_removal(manager):
    manager.reload(settings(alice_keys=[key("old", ALICE), key("new", ALICE_NEW)]))
    for token in [ALICE, ALICE_NEW]:
        assert manager.authenticate("Bearer "+token, "gpt-test").client_id == "alice"
    manager.reload(settings(alice_keys=[key("old", ALICE, enabled=False), key("new", ALICE_NEW)]))
    with pytest.raises(AuthError):
        manager.authenticate("Bearer "+ALICE, "gpt-test")
    manager.reload(settings(alice_keys=[key("new", ALICE_NEW)]))
    with pytest.raises(AuthError):
        manager.authenticate("Bearer "+ALICE, "gpt-test")
    assert manager.authenticate("Bearer "+ALICE_NEW, "gpt-test").client_id == "alice"


def test_upstream_round_robin_never_selects_disabled_or_client_key(manager):
    assert [manager.resolve_upstream_key("openai") for _ in range(5)] == [
        UPSTREAM_1, UPSTREAM_2, UPSTREAM_1, UPSTREAM_2, UPSTREAM_1,
    ]
    assert manager.authenticate("Bearer "+ALICE, "gpt-test").client_id == "alice"


@pytest.mark.parametrize("ref", ["missing", "", None, [], "openai\n"])
def test_unknown_upstream_pool_is_safe_503(manager, ref):
    with pytest.raises(UpstreamKeyUnavailable) as caught:
        manager.resolve_upstream_key(ref)
    assert caught.value.status_code == 503
    body = json.loads(caught.value.to_response().body)
    assert body["error"]["type"] == "server_error"
    assert body["error"]["code"] == "upstream_key_unavailable"
    assert "openai" not in str(caught.value)


def test_empty_and_disabled_upstream_pools_fail_closed_and_rotation_applies(manager):
    for keys in [[], [UpstreamKeyConfig(key_id="off", secret=UPSTREAM_1, enabled=False)]]:
        manager.reload(settings(pools=[UpstreamPoolConfig(key_ref="openai", keys=keys)]))
        with pytest.raises(UpstreamKeyUnavailable):
            manager.resolve_upstream_key("openai")
    manager.reload(settings(pools=[UpstreamPoolConfig(key_ref="openai", keys=[
        UpstreamKeyConfig(key_id="new", secret=UPSTREAM_2),
    ])]))
    assert manager.resolve_upstream_key("openai") == UPSTREAM_2
    manager.reload(AuthConfig())
    with pytest.raises(AuthError):
        manager.authenticate("Bearer "+ALICE, "gpt-test")
    with pytest.raises(UpstreamKeyUnavailable):
        manager.resolve_upstream_key("openai")


def test_config_mapping_is_snapshot_and_redacted(caplog):
    source = {"clients": [{"client_id": "a", "keys": [{"key_id": "k", "sha256": hash_client_key(ALICE)}],
                           "allowed_models": ["gpt-test"]}],
              "upstream_pools": [{"key_ref": "openai", "keys": [{"key_id": "p", "secret": UPSTREAM_1}]}]}
    config = AuthConfig.from_mapping(source)
    source["clients"][0]["allowed_models"].append("claude-test")
    source["clients"][0]["keys"].clear()
    source["upstream_pools"][0]["keys"][0]["secret"] = "changed"
    manager = AuthManager(config)
    assert manager.authenticate("Bearer "+ALICE, "gpt-test").allowed_models == frozenset({"gpt-test"})
    assert manager.resolve_upstream_key("openai") == UPSTREAM_1
    diagnostics = repr(config)+repr(manager)+json.dumps(asdict(config), default=str)+caplog.text
    assert ALICE not in diagnostics
    assert UPSTREAM_1 not in diagnostics
    assert "**********" in diagnostics


@pytest.mark.parametrize("factory", [
    lambda: ClientKeyConfig(key_id="k", sha256=ALICE),
    lambda: ClientConfig(client_id="bad\n", keys=[]),
    lambda: ClientConfig(client_id="a", allowed_models="gpt-test"),
    lambda: ClientConfig(client_id="a", allowed_models=["bad\n"]),
    lambda: ClientConfig(client_id="a", keys="not-a-list"),
    lambda: ClientConfig(client_id="a", enabled="false"),
    lambda: UpstreamKeyConfig(key_id="p", secret=""),
    lambda: UpstreamKeyConfig(key_id="p", secret="${MISSING_API_KEY}"),
    lambda: UpstreamKeyConfig(key_id="p", secret=UPSTREAM_1+"\r\nX: y"),
    lambda: UpstreamKeyConfig(key_id="p", secret=17),
    lambda: AuthConfig.from_mapping({"clients": "single-client"}),
    lambda: AuthConfig.from_mapping({"clients": [ALICE]}),
    lambda: AuthConfig.from_mapping({"unexpected": ALICE}),
    lambda: AuthConfig.from_mapping({"clients": [{"client_id": "a", "enabled": "false"}]}),
    lambda: AuthConfig.from_mapping({"upstream_pools": [{"key_ref": "p", "keys": [{"key_id": "k", "secret": UPSTREAM_1, "typo": UPSTREAM_2}]}]}),
])
def test_invalid_configuration_fails_with_redacted_error(factory):
    with pytest.raises(AuthConfigurationError) as caught:
        factory()
    for secret in [ALICE, UPSTREAM_1, UPSTREAM_2]:
        assert secret not in str(caught.value)+repr(caught.value)


def test_duplicate_credentials_and_cross_role_reuse_are_rejected():
    a = ClientConfig(client_id="a", keys=[key("a", ALICE)])
    invalid = [
        {"clients": [a, a]},
        {"clients": [a, ClientConfig(client_id="b", keys=[key("b", ALICE, enabled=False)])]},
        {"clients": [a, ClientConfig(client_id="b", keys=[key("a", BOB)])]},
        {"clients": [a], "upstream_pools": [UpstreamPoolConfig(key_ref="p", keys=[UpstreamKeyConfig(key_id="p", secret=ALICE)])]},
        {"upstream_pools": [UpstreamPoolConfig(key_ref="p"), UpstreamPoolConfig(key_ref="p")]},
    ]
    for fields in invalid:
        with pytest.raises(AuthConfigurationError):
            AuthConfig(**fields)
    with pytest.raises(AuthConfigurationError):
        UpstreamPoolConfig(key_ref="p", keys=[UpstreamKeyConfig(key_id="k", secret=UPSTREAM_1), UpstreamKeyConfig(key_id="k", secret=UPSTREAM_2)])


@pytest.mark.parametrize("token", [None, "", "a b", "a\nb", "\u2603", "a=b", "x"*4097])
def test_hash_helper_rejects_non_bearer_tokens(token):
    with pytest.raises(AuthConfigurationError):
        hash_client_key(token)


def test_failed_reload_preserves_active_snapshot(manager):
    with pytest.raises(AuthConfigurationError):
        manager.reload({"clients": []})
    assert manager.authenticate("Bearer "+ALICE, "gpt-test").client_id == "alice"
    assert manager.resolve_upstream_key("openai") == UPSTREAM_1


def test_concurrent_pool_selection_and_snapshot_reload(manager):
    def resolve(_):
        assert manager.authenticate("Bearer "+ALICE, "gpt-test").client_id == "alice"
        return manager.resolve_upstream_key("openai")
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(resolve, range(200)))
    assert results.count(UPSTREAM_1) == results.count(UPSTREAM_2) == 100

    config = settings()
    def swap_or_read(index):
        if index % 5 == 0:
            manager.reload(config)
        else:
            assert manager.authenticate("Bearer "+BOB, "claude-test").client_id == "bob"
            assert manager.resolve_upstream_key("openai") in {UPSTREAM_1, UPSTREAM_2}
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(swap_or_read, range(200)))


def test_http_boundary_statuses_and_upstream_header_separation(manager, caplog):
    # A small consumer exercises the actual library's HTTP error responses.
    # Production main/router wiring is owned by GW-006 and is deliberately not
    # modified or claimed to be secured by this module-level fixture.
    app = FastAPI()
    received = []

    def upstream(request):
        received.append(request.headers["authorization"])
        assert request.headers["authorization"] != "Bearer "+ALICE
        return httpx.Response(200, json={"choices": []})

    @app.post("/v1/chat/completions")
    async def route(request: Request):
        values = request.headers.getlist("authorization")
        body = await request.json()
        try:
            identity = manager.authenticate(values[0] if len(values) == 1 else None, body["model"])
            secret = manager.resolve_upstream_key("openai")
        except (AuthError, UpstreamKeyUnavailable) as error:
            return error.to_response()
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            response = await client.post("https://provider.invalid/v1/chat/completions", json=body,
                                         headers={"Authorization": "Bearer "+secret})
        return {"client_id": identity.client_id, "result": response.json()}

    with TestClient(app) as client:
        for header in [None, "Bearer unknown", "Bearer "+REVOKED]:
            response = client.post("/v1/chat/completions", json={"model": "gpt-test"},
                                   headers={"Authorization": header} if header else {})
            assert response.status_code == 401
            assert response.json()["error"]["code"] == "invalid_api_key"
        assert client.post("/v1/chat/completions", json={"model": "claude-test"},
                           headers={"Authorization": "Bearer "+ALICE}).status_code == 403
        assert client.post("/v1/chat/completions", json={"model": "gpt-test"},
                           headers=[("Authorization", "Bearer "+ALICE), ("Authorization", "Bearer "+BOB)]).status_code == 401
        assert received == []
        good = client.post("/v1/chat/completions", json={"model": "gpt-test"}, headers={"Authorization": "Bearer "+ALICE})
        assert good.status_code == 200 and good.json()["client_id"] == "alice"
        assert received == ["Bearer "+UPSTREAM_1]
    for secret in [ALICE, REVOKED, UPSTREAM_1, UPSTREAM_2]:
        assert secret not in caplog.text
