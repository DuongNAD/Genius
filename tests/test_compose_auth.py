"""docker-compose.yml deployment contract (P1 fix).

Compose only uses a sibling .env for ${...} interpolation — it never injects
it into containers, and .dockerignore keeps .env out of the image. So unless
every service carries an explicit SKILL_API_KEY environment entry, the hub
and skill servers boot fine and then reject every authenticated request —
the exact failure this file pins against regressions. Also pins the port
posture (agent servers published loopback-only) and per-service healthchecks.
"""

import os

import yaml

_COMPOSE_PATH = os.path.join(os.path.dirname(__file__), "..", "docker-compose.yml")

AGENT_SERVICES = (
    "grok_researcher",
    "claude_architect",
    "codex_reviewer",
    "tester_agent",
    "security_agent",
    "devops_agent",
)


def _compose():
    with open(_COMPOSE_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_every_service_gets_skill_api_key():
    services = _compose()["services"]
    assert set(services) == {"hub", "dashboard", *AGENT_SERVICES}
    for name, svc in services.items():
        env = svc.get("environment") or []
        entries = [str(e) for e in env]
        assert any(e.startswith("SKILL_API_KEY=") for e in entries), (
            f"service {name} must pass SKILL_API_KEY into the container"
        )


def test_skill_api_key_fails_fast_when_unset():
    # The `:?` interpolation form makes `docker compose up` refuse to start
    # with the secret unset, instead of booting services that reject
    # every authenticated request.
    services = _compose()["services"]
    for name, svc in services.items():
        entry = next(
            str(e)
            for e in (svc.get("environment") or [])
            if str(e).startswith("SKILL_API_KEY=")
        )
        assert ":?" in entry, f"service {name} should use the ${{VAR:?}} form"


def test_agent_ports_published_loopback_only():
    services = _compose()["services"]
    for name in AGENT_SERVICES:
        ports = [str(p) for p in services[name].get("ports") or []]
        assert ports, f"service {name} should still publish for the host orchestrator"
        for port in ports:
            assert port.startswith("127.0.0.1:"), (
                f"service {name} port {port} must bind loopback — the agents "
                "are for the orchestrator/hub, not the LAN"
            )


def test_every_service_has_a_healthcheck():
    services = _compose()["services"]
    for name, svc in services.items():
        assert "healthcheck" in svc, f"service {name} needs a healthcheck"
        test_cmd = svc["healthcheck"]["test"]
        assert test_cmd[0] == "CMD", name
        # python-based probes: the slim image ships no curl.
        assert test_cmd[1] == "python", name


def test_agent_healthchecks_probe_their_own_port():
    services = _compose()["services"]
    expected_port = {
        "grok_researcher": "8001",
        "claude_architect": "8002",
        "codex_reviewer": "8003",
        "tester_agent": "8004",
        "security_agent": "8005",
        "devops_agent": "8006",
    }
    for name, port in expected_port.items():
        probe = " ".join(services[name]["healthcheck"]["test"])
        assert f":{port}/health" in probe, (name, probe)
