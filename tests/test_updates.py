"""The update path, with the registry and Watchtower stood in for.

Everything answers through mock transports: the registry walk from tag to config label,
the anonymous token dance, the probe that must never trigger an update, and the trigger
whose timeout is the success case.
"""

from __future__ import annotations

import time
from pathlib import Path

import httpx
import pytest

from email_to_cal import updates
from email_to_cal.config import Settings
from email_to_cal.updates import ImageRef, Published, UpdateStatus, parse_image

RUNNING = "a" * 40
NEWER = "b" * 40

REF = ImageRef("ghcr.io", "sebastienstdenis/email-to-cal", "latest")

# An index the way buildx publishes one: the attestation manifests ride along under
# platform unknown/unknown, and stand first here to prove they are stepped over.
INDEX = {
    "mediaType": "application/vnd.oci.image.index.v1+json",
    "manifests": [
        {"digest": "sha256:att", "platform": {"os": "unknown", "architecture": "unknown"}},
        {"digest": "sha256:amd", "platform": {"os": "linux", "architecture": "amd64"}},
    ],
}

MANIFEST = {"config": {"digest": "sha256:cfg"}}

CONFIG = {
    "config": {
        "Labels": {
            "org.opencontainers.image.revision": NEWER,
            "org.opencontainers.image.version": "v321",
        }
    }
}


@pytest.fixture(autouse=True)
def calm_module_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts from an unchecked cache, whatever ran before it."""
    monkeypatch.setattr(updates, "_status", UpdateStatus(running=updates.RUNNING_SHA))


def registry(asked: list[str] | None = None) -> httpx.MockTransport:
    """A registry that insists on the anonymous token dance before answering."""

    def handle(request: httpx.Request) -> httpx.Response:
        if asked is not None:
            asked.append(request.url.path)
        if request.url.path == "/token":
            assert request.url.params["scope"] == "repository:sebastienstdenis/email-to-cal:pull"
            return httpx.Response(200, json={"token": "anonymous"})
        if request.headers.get("Authorization") != "Bearer anonymous":
            return httpx.Response(
                401,
                headers={
                    "www-authenticate": 'Bearer realm="https://ghcr.io/token",'
                    'service="ghcr.io",scope="repository:sebastienstdenis/email-to-cal:pull"'
                },
            )
        # The blob comes back as a redirect onto a CDN, the way ghcr.io answers, so a
        # client that does not follow redirects fails here the way it fails for real.
        if request.url.path == "/v2/sebastienstdenis/email-to-cal/blobs/sha256:cfg":
            return httpx.Response(307, headers={"location": "/cdn/sha256:cfg"})
        answers = {
            "/v2/sebastienstdenis/email-to-cal/manifests/latest": INDEX,
            "/v2/sebastienstdenis/email-to-cal/manifests/sha256:amd": MANIFEST,
            "/cdn/sha256:cfg": CONFIG,
        }
        payload = answers.get(request.url.path)
        if payload is None:
            return httpx.Response(404)
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(handle)


def watchtower(settings: Settings) -> Settings:
    settings.watchtower_url = "watchtower:8080"
    settings.watchtower_token = "secret"
    return settings


def test_a_full_ref_splits_into_its_parts() -> None:
    assert parse_image("ghcr.io/sebastienstdenis/email-to-cal:latest") == REF


def test_a_ref_without_a_tag_means_latest() -> None:
    assert parse_image("ghcr.io/sebastienstdenis/email-to-cal").tag == "latest"


def test_a_bare_hub_name_is_not_read_as_a_registry() -> None:
    assert parse_image("containrrr/watchtower") == ImageRef(
        "registry-1.docker.io", "containrrr/watchtower", "latest"
    )
    assert parse_image("postgres:16") == ImageRef("registry-1.docker.io", "library/postgres", "16")


def test_the_watchtower_filter_is_the_ref_without_the_tag() -> None:
    assert REF.name == "ghcr.io/sebastienstdenis/email-to-cal"


def test_the_build_is_read_off_the_image_config() -> None:
    with httpx.Client(transport=registry(), follow_redirects=True) as client:
        assert updates.published_build(REF, client) == Published(NEWER, "v321")


def test_the_token_dance_happens_once_for_the_whole_walk() -> None:
    asked: list[str] = []
    with httpx.Client(transport=registry(asked), follow_redirects=True) as client:
        updates.published_build(REF, client)
    assert asked.count("/token") == 1


def test_index_annotations_answer_without_the_walk() -> None:
    annotated = {
        "manifests": INDEX["manifests"],
        "annotations": {
            "org.opencontainers.image.revision": NEWER,
            "org.opencontainers.image.version": "v321",
        },
    }
    asked: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.path)
        return httpx.Response(200, json=annotated)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert updates.published_build(REF, client) == Published(NEWER, "v321")
    assert asked == ["/v2/sebastienstdenis/email-to-cal/manifests/latest"]


def test_a_version_merely_echoing_the_tag_is_not_one() -> None:
    """The metadata step used to derive the version label from the tag itself."""
    annotated = {
        "annotations": {
            "org.opencontainers.image.revision": NEWER,
            "org.opencontainers.image.version": "latest",
        }
    }

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=annotated)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert updates.published_build(REF, client) == Published(NEWER, None)


def test_an_unlabelled_image_answers_none_rather_than_raising() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/v2/sebastienstdenis/email-to-cal/blobs/"):
            return httpx.Response(200, json={"config": {}})
        return httpx.Response(200, json={"config": {"digest": "sha256:cfg"}, "bare": True})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert updates.published_build(REF, client) == Published(None, None)


def test_available_is_honest_about_what_it_cannot_compare() -> None:
    assert UpdateStatus(running="", latest=NEWER).available is None
    assert UpdateStatus(running=RUNNING, latest=None).available is None
    assert UpdateStatus(running=RUNNING, latest=RUNNING).available is False
    assert UpdateStatus(running=RUNNING, latest=NEWER).available is True


def test_the_scheme_nobody_types_is_put_on(settings: Settings) -> None:
    assert updates.api_base(watchtower(settings)) == "http://watchtower:8080"
    settings.watchtower_url = "https://updates.example.com/"
    assert updates.api_base(settings) == "https://updates.example.com"


def test_the_probe_proves_the_token_through_metrics(settings: Settings) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/metrics"
        assert request.headers["Authorization"] == "Bearer secret"
        return httpx.Response(200, text="watchtower_containers_updated 0")

    ok, detail = updates.probe(watchtower(settings), transport=httpx.MockTransport(handle))
    assert ok
    assert detail == "token accepted"


def test_the_probe_names_a_rejected_token(settings: Settings) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    ok, detail = updates.probe(watchtower(settings), transport=httpx.MockTransport(handle))
    assert not ok
    assert "rejected" in detail


def test_the_probe_never_sends_the_real_token_at_the_update_endpoint(
    settings: Settings,
) -> None:
    """With metrics off, the fallback knock must not be able to start an update.

    The stand-in answers the way the maintained fork (nickfedor/watchtower) does: the
    update route exists for POST alone and refuses any other method before it ever
    reads the token, so a probe that knocked with GET would fail here on a 405.
    """
    tokens: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/metrics":
            return httpx.Response(404)
        assert request.url.path == "/v1/update"
        if request.method != "POST":
            return httpx.Response(405)
        tokens.append(request.headers["Authorization"])
        return httpx.Response(401)

    ok, detail = updates.probe(watchtower(settings), transport=httpx.MockTransport(handle))
    assert ok
    assert "Bearer secret" not in tokens
    assert "proven by an update" in detail


def test_the_probe_says_when_nothing_is_listening(settings: Settings) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    ok, detail = updates.probe(watchtower(settings), transport=httpx.MockTransport(refuse))
    assert not ok
    assert "could not reach Watchtower" in detail


def test_a_quick_answer_means_nothing_was_pulled(settings: Settings) -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    outcome = updates.trigger(watchtower(settings), transport=httpx.MockTransport(handle))
    assert outcome.ok and not outcome.restarting
    (request,) = seen
    assert request.url.path == "/v1/update"
    assert request.url.params["image"] == "ghcr.io/sebastienstdenis/email-to-cal"
    assert request.headers["Authorization"] == "Bearer secret"


def test_a_refusal_comes_back_as_one(settings: Settings) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    outcome = updates.trigger(watchtower(settings), transport=httpx.MockTransport(handle))
    assert not outcome.ok
    assert "token" in outcome.detail


def test_a_busy_watchtower_is_a_message_rather_than_a_number(settings: Settings) -> None:
    """The fork holds one update at a time and answers 429 while it does."""

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "30"})

    outcome = updates.trigger(watchtower(settings), transport=httpx.MockTransport(handle))
    assert not outcome.ok
    assert "busy" in outcome.detail


def test_an_accepted_update_is_watched_rather_than_called_done(settings: Settings) -> None:
    """202 is the fork's async mode: started, outcome unknown, so it reads as silence."""

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(202)

    outcome = updates.trigger(watchtower(settings), transport=httpx.MockTransport(handle))
    assert outcome.ok and outcome.restarting


def test_silence_is_read_as_the_update_arriving(settings: Settings) -> None:
    def hang(request: httpx.Request) -> httpx.Response:
        time.sleep(0.2)
        return httpx.Response(200)

    outcome = updates.trigger(watchtower(settings), wait=0.01, transport=httpx.MockTransport(hang))
    assert outcome.ok and outcome.restarting
    # The request was left running rather than cancelled; let it land.
    time.sleep(0.3)


def test_a_failed_registry_read_is_not_stamped_as_fresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(updates, "_status", UpdateStatus(running=RUNNING))

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to registry")

    state = updates.status(transport=httpx.MockTransport(refuse))
    assert state.error is not None
    assert state.checked is None

    state = updates.status(transport=registry())
    assert state.latest == NEWER
    assert state.latest_version == "v321"
    assert state.error is None
    assert state.checked is not None


def test_a_fresh_answer_is_not_asked_for_again(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(updates, "_status", UpdateStatus(running=RUNNING))
    asked: list[str] = []
    state = updates.status(transport=registry(asked))
    assert state.latest == NEWER
    walked = len(asked)

    again = updates.status(transport=registry(asked))
    assert again is state
    assert len(asked) == walked


def test_force_asks_the_registry_past_a_fresh_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """The refresh icon means now, so a fresh cache does not stand in for the registry."""
    monkeypatch.setattr(updates, "_status", UpdateStatus(running=RUNNING))
    asked: list[str] = []
    updates.status(transport=registry(asked))
    walked = len(asked)

    again = updates.status(force=True, transport=registry(asked))
    assert again.latest == NEWER
    assert len(asked) > walked


def test_the_compose_file_and_the_default_image_agree() -> None:
    """The IMAGE constant is what the button updates; compose is what deploys it."""
    compose = (Path(__file__).parents[1] / "docker-compose.yml").read_text()
    assert updates.parse_image(updates.IMAGE).name in compose
