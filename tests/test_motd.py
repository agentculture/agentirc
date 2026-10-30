"""CULTURE.DEV greeting: welcome/yourhost wording and MOTD numerics."""

import pytest


async def _register(client, nick="testserv-ori"):
    await client.send(f"NICK {nick}")
    await client.send("USER ori 0 * :Ori")
    return await client.recv_all(timeout=1.0)


def _by_code(lines):
    out = {}
    for line in lines:
        parts = line.split(" ", 3)
        if len(parts) >= 2:
            out.setdefault(parts[1], []).append(line)
    return out


@pytest.mark.asyncio
async def test_welcome_and_yourhost_name_culture_dev(server, make_client):
    codes = _by_code(await _register(await make_client()))
    welcome = codes["001"][0]
    assert "Welcome to CULTURE.DEV on testserv " in welcome
    assert welcome.rstrip().endswith("testserv-ori!ori@127.0.0.1")
    assert "™" not in welcome
    yourhost = codes["002"][0]
    assert "Your host is testserv, running CULTURE.DEV" in yourhost
    assert "™" not in yourhost


@pytest.mark.asyncio
async def test_myinfo_version_token_unchanged(server, make_client):
    codes = _by_code(await _register(await make_client()))
    assert codes["004"][0].split()[4] == "culture"


@pytest.mark.asyncio
async def test_motd_sent_on_registration(server, make_client):
    lines = await _register(await make_client())
    codes = [line.split()[1] for line in lines]
    assert "375" in codes and "376" in codes
    assert codes.index("375") < codes.index("372") < codes.index("376")
    assert codes.index("004") < codes.index("375")
    body = "\n".join(_by_code(lines)["372"])
    assert "CULTURE.DEV" in body
    assert "https://culture.dev" in body
    assert "™" not in "\n".join(lines)


@pytest.mark.asyncio
async def test_motd_command(server, make_client):
    client = await make_client()
    await _register(client)
    await client.send("MOTD")
    lines = await client.recv_all(timeout=1.0)
    codes = [line.split()[1] for line in lines]
    assert codes[0] == "375" and codes[-1] == "376"
    assert "372" in codes
    assert any("CULTURE.DEV" in line for line in lines)


@pytest.mark.asyncio
async def test_motd_before_registration_not_served(server, make_client):
    client = await make_client()
    await client.send("MOTD")
    lines = await client.recv_all(timeout=0.5)
    assert not any(" 375 " in line or " 376 " in line for line in lines)
