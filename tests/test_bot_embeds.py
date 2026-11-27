import discord
import AI_Bot


def test_get_help_manifest_returns_structured_embed(monkeypatch):
    monkeypatch.setattr(AI_Bot, "BOT_NAME", "Atlas")
    monkeypatch.setattr(
        AI_Bot,
        "AUDIO_COMMANDS",
        {"!ping": {"file": "ping.mp3", "label": "Sonar Ping"}},
    )

    embed = AI_Bot.get_help_manifest()
    assert isinstance(embed, discord.Embed)
    assert "ATLAS COMMAND & RELAY DIRECTORY" in embed.title
    assert embed.color == discord.Color.teal()
    assert len(embed.fields) == 3
    assert "`!status`" in embed.fields[0].value
    assert "`!transmit`" in embed.fields[1].value
    assert "`!ping`" in embed.fields[2].value


def test_build_transmit_embed_source_badges_and_truncation():
    long_text = "A" * 4500

    local_embed = AI_Bot.build_transmit_embed(long_text, "LOCAL_CORE")
    assert isinstance(local_embed, discord.Embed)
    assert local_embed.color == discord.Color.blue()
    assert "Local Neural Core" in local_embed.footer.text
    # Enforces Discord's 4096-char description ceiling safely
    assert len(local_embed.description) == 4000

    cloud_embed = AI_Bot.build_transmit_embed("Relay active.", "CLOUD_RELAY")
    assert cloud_embed.color == discord.Color.purple()
    assert "Cloud Fallback Relay" in cloud_embed.footer.text

    archive_embed = AI_Bot.build_transmit_embed("Cached log.", "ARCHIVE_FALLBACK")
    assert archive_embed.color == discord.Color.dark_grey()
    assert "Static Archive Fragment" in archive_embed.footer.text
