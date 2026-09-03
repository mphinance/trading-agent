# Voice Stack Guide — SUPERSEDED (2026-09-03)

The Telegram-voice-note pipeline this file used to describe (cloud STT, a
DeepSeek-V4 brain step, Kokoro-82M TTS) is cancelled. It will not be built.

Voice, as it actually exists now, is claude.ai's own voice mode calling
`trading_mcp` tools over the MCP connector. There is no STT, no TTS, and no
audio endpoint anywhere in this repo, and none is planned.

The old design was dropped because it added a whole subsystem (an STT hop, a
TTS hop, and a second voice surface to maintain) to solve a problem claude.ai
had already solved: voice mode already works with connectors on mobile, so
building a parallel Telegram voice path was duplicate work for no gain.

One constraint from the old design survives unchanged: approvals never become
a voice command. Inline Telegram/Discord buttons remain the only way a
proposal is approved, because a transcript is ambiguous exactly where it
matters most (a spoken "approve" does not say which proposal).

See `docs/NEXT_STEPS.md` for current status and `CLAUDE.md` for the binding
rules this repo still follows.
