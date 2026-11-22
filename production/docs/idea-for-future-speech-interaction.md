# Idea: Speech Interaction for the ChatBot (Voice Input / Voice Output / Wake Word)

> **Status: concept, not yet implemented.** This document records the design
> and a researched tool selection (verified against current GitHub and web
> sources on 2026-10-27/28). Nothing here exists in the code yet.

## Goal

Let users **talk to the chatbot** instead of (or in addition to) typing:

1. **Voice input** — the user speaks a question; it is transcribed and sent
   through the existing chat path.
2. **Voice output** — the chatbot answers audibly.
3. **Constant listening with wake word** — the chatbot keeps its "ears"
   open during a session. If the session has been idle for a while
   (target: ~2–3 minutes), it drops to a low-attention state and only
   wakes up again on a wake phrase such as **"Hello CityBot"** or
   **"Hello Chatbot"**.

### Hard requirements

- **No API keys, no registrations, no external services.** Everything runs
  inside the deployment. This rules out cloud STT/TTS APIs, the browser's
  built-in `SpeechRecognition` (which phones home to the browser vendor's
  cloud in Chrome/Edge), and wake-word engines that require an access key
  (e.g. Picovoice Porcupine).
- **Both English and German**, ideally switchable per utterance / per
  session, since the user base is German-speaking with English technical
  vocabulary (CityGML, LoD, EPSG, …).
- **Optional feature** — gated by `.env` (e.g. `VOICE_ENABLED=false`),
  off by default, like `ENABLE_MEMORY` and the other experimental features.
  When disabled, the UI and the deployment are exactly as they are today.

## Current Implementation (integration seams)

All speech processing would live **outside** the existing webui; the seams
that make this possible already exist:

- **Chat entry point** — `chat_stream()` in `production/webui/app.py`
  (line ~787) is wired to `msg_input.submit(fn=chat_stream, …)` and
  `send_btn.click(…)` (line ~3686). A transcribed utterance only needs to
  fill the text box and trigger this existing path; no new chat/LLM logic
  is required.
- **Global browser JS** — the UI already injects a large script via
  `gr.Blocks(js=_RESIZE_JS, head=…)` (line ~3002) because `gr.HTML` strips
  `<script>` tags. The audio capture/playback logic belongs in this same
  global-JS seam, and the existing code already solves the hard part of
  surviving Gradio's streaming re-renders (see the chat-scroll/observer
  handling in `app.py`).
- **Extra HTTP endpoints** — the Gradio app is mounted into a FastAPI
  instance (`app.py` line ~3938) that already serves `/api/…` routes;
  nothing here is needed if the voice service runs as a separate
  container, but it is the escape hatch if not.
- **Conditional containers** — `production/docker-compose.fullstack.yml`
  already contains pre-pull-only helper images gated by `profiles`
  (`citydb-tool`, `ifc-to-citygml3`); a `voice` service can follow the
  same pattern, started only when configured.
- **Configuration** — `production/.env.example` already documents the
  provider/base-URL conventions; `VOICE_*` keys slot in naturally.

## Tool Selection (research results, verified 2026-10-27/28)

Star counts and last-push dates were read from the GitHub API on
2026-10-27/28; the rankings cross-check at least three independent
"best of 2026" roundups (several are vendor-adjacent — AssemblyAI,
Gladia, Picovoice — so agreement between independent sources was the
acceptance criterion).

### Speech-to-text (STT)

| Engine | Stars | Last push | License | Notes |
|--------|-------|-----------|---------|-------|
| `ggml-org/whisper.cpp` | 54.7k | 2026-10-27 | MIT | C/C++, quantized models down to 4-bit, the community default for CPU real-time; also supports an Ascend NPU backend and a WebAssembly target |
| `SYSTRAN/faster-whisper` | 26.2k | 2026-10-25 | MIT | Python (CTranslate2); same Whisper weights, ~4× faster than the reference implementation, built-in Silero-VAD integration; shines on NVIDIA GPUs |
| `alphacep/vosk-api` | 15.2k | 2026-10-15 | Apache-2.0 | True token-streaming ASR, tiny models, low accuracy; the Home Assistant default |
| `openai/whisper` | 111k | 2026-08-31 | MIT | Reference implementation — slow, non-streaming, not for production |

The consensus decision rule across all comparisons: **whisper.cpp for
plain-CPU real-time use, faster-whisper for GPU batch throughput.** Both
run the identical Whisper weights, so accuracy is set by quantization
(INT8 is the safe midpoint) and by voice-activity gating, not by the
wrapper.

**Pick: `whisper.cpp` with the multilingual `base` model at INT8**
(see the EN/DE section below). `faster-whisper` is the documented
fallback if staying 100%-Python for the Dockerfile is preferred.

### Text-to-speech (TTS)

| Engine | Stars | Last push | License | CPU? |
|--------|-------|-----------|---------|------|
| `hexgrad/kokoro` (Kokoro-82M) | 9.7k | 2025-08-06 | Apache-2.0 | ✅ ~2–3 GB |
| `thewh1teagle/kokoro-onnx` | 2.8k | 2026-09-01 | Apache-2.0 | ✅ |
| `OHF-Voice/piper1-gpl` (active Piper fork) | 6.1k | 2026-10-25 | GPL-3.0 | ✅ <1 GB, near real-time |
| `rhasspy/piper` | 11.3k | — | **archived 2025-10-06** | superseded by `piper1-gpl` |

Other 2026-wave candidates were **rejected for this project**: Chatterbox
(0.5B, MIT, excellent quality, but ~4–6 GB and GPU-oriented, plus an
embedded PerTh audio watermark), XTTS v2 (CPML — non-commercial only),
F5-TTS / Fish Speech / ChatTTS (non-commercial licenses), Qwen3-TTS
(Apache-2.0 but GPU-oriented).

Important nuance: the main Kokoro repo has had no pushes since
Aug 2025, but the ecosystem is healthy via `kokoro-onnx` (Sep 2026) plus
the `misaki` G2P library and an `espeak-ng` system dependency.

### Wake word

| Engine | Stars | Last push | License | Notes |
|--------|-------|-----------|---------|-------|
| `dscripka/openWakeWord` | 2.9k | 2025-12-30 | MIT | Pretrained phrases + **offline custom-phrase training** (documented notebook, <1 h on CPU); includes web streaming examples |
| `OHF-Voice/micro-wake-word` | 1.0k | 2026-07-06 | MIT (code) | 33 fixed phrases, no custom training, ESP32-oriented |
| Picovoice Porcupine | ~5.5k | active | proprietary | Excluded: free tier requires an access key/registration |

**Pick: `openWakeWord`** — MIT, actively maintained (my earlier concern
about dormancy was unfounded), and the only key-less option that can
train a *custom* phrase like "hello citybot". Fallback if the custom
model proves flaky: in the low-attention state, run the STT engine on
short VAD-gated clips and regex-match the wake phrase (slightly less
robust, zero extra dependency).

### Voice activity detection (endpointing)

**`snakers4/silero-vad`** (10.5k ⭐, pushed 2026-10-22) — the de-facto
standard, ONNX-based, runs in the same Python process. Note the
**CC BY-NC 4.0** license (fine for this deployment; must be documented if
the stack is ever commercialized).

## English + German Support

This is the deciding constraint for TTS and shapes the other picks:

| Component | English | German | How |
|-----------|---------|--------|-----|
| STT (whisper.cpp) | ✅ | ✅ | Use the **multilingual `base`** model — *not* the English-only `base.en`. Language is auto-detected per utterance, so a user may switch between German and English (and mix technical terms) freely. |
| TTS (piper1-gpl) | ✅ | ✅ | Officially ships **`de_DE`** voices plus **`en_GB`/`en_US`** (verified in its `docs/VOICES.md` and the `rhasspy/piper-voices` HF collection). Three small models (~60 MB each) can be preloaded in the image, selected per session. |
| TTS (Kokoro, optional) | ✅ | ⚠️ community only | The 54 official voices cover 9 languages **without German**. German exists only as community fine-tunes (`semidark/kokoro-deutsch`, `Godelaune/Kokoro-82M-ONNX-German-Martin`) — fine for A/B tests, not for the default path. |
| Wake word (openWakeWord) | ✅ | ⚠️ | Trained on English audio (LibriSpeech). The wake phrase should therefore stay **English** ("Hello CityBot") — which is what we want anyway. A German "Hallo CityBot" would be out-of-distribution; the whisper-transcript fallback would cover it if ever needed. |

Consequence: **piper1-gpl is the default TTS engine** for the bilingual
setup (Kokoro is the optional English-quality upgrade), even though
Kokoro sounds noticeably better — this is the only first-class EN+DE CPU
TTS that meets the no-key rule. Quality expectation to manage: Piper is
"clear and pleasant", not "natural human".

## Proposed Architecture

A new **`citydb-voice`** sidecar container; the browser is reduced to a
thin mic/ear. Nothing in the MCP server or the chat runtime changes.

```
Browser (Gradio UI)                      citydb-voice container
─────────────────────                    ──────────────────────────────
AudioWorklet captures 16 kHz mono PCM
   │  one persistent WebSocket (up: audio, down: events/text/audio)
   ├──────────────────────────────────►  openWakeWord  (wake phrase)
   │                                     Silero VAD    (utterance ends)
   │                                     whisper.cpp   (STT, multilingual base)
   │◄───────────────────────────────────  transcript
   │
   │  fills the text box → existing submit path (app.py:3686)
   ├──────────────────────────────────────────────►  chat_stream → LLM
   │
   │  POST /tts {text, voice}
   ├──────────────────────────────────►  piper1-gpl → 24 kHz WAV chunks
   │◄───────────────────────────────────  played via AudioContext
```

- **Sidecar** — one Python service (uvicorn + WebSocket + a `/tts` POST
  + `/health`), containing faster-whisper-adjacent components:
  whisper.cpp (via its server or bindings), piper1-gpl, openWakeWord,
  Silero VAD. Model weights are baked into the image (or downloaded at
  first start into a named volume). CPU-only; realistic footprint
  ~1–2 GB RAM.
- **Compose** — a `voice` service in `docker-compose.fullstack.yml`
  (and documented for `docker-compose.byod.yml`), started only when
  `VOICE_ENABLED=true` (env gate, `profiles`-style). No extra image
  running when the feature is off.
- **WebUI** — reads `VOICE_ENABLED` / `VOICE_HOST:PORT`, health-checks
  the sidecar on startup, and only then renders the mic/voice controls
  and activates the audio JS. Unreachable sidecar ⇒ zero UI change.
- **`.env` keys** — `VOICE_ENABLED=false`, `VOICE_HOST`/`VOICE_PORT`
  (default `voice:9800`), `VOICE_STT_MODEL=base`, `VOICE_TTS_ENGINE=piper`,
  `VOICE_TTS_VOICES=en_GB,de_DE`, `VOICE_WAKE_PHRASE="hello citybot"`,
  `VOICE_IDLE_TIMEOUT=150`.

### Listening state machine (constant listening + wake word)

```
                  ┌─────────────────────────────────────────────┐
                  │                                             │
   session start  │        ┌────────┐  user speaks   ┌────────┐ │
  ────────────────┼───────►│ AWAKE  │────────────────►│LISTEN │ │
                  │        └───┬────┘                 └───┬────┘ │
                  │   any utterance is a question         │ answer done
                  │            │ idle > 2–3 min           │
                  │            ▼                          │
                  │        ┌──────────┐                   │
                  │        │ SLEEPING │◄──────────────────┘  (stays AWAKE for
                  │        └────┬─────┘                     the idle window)
                  │   only the wake phrase is accepted
                  └─────────────┘
```

- The browser keeps one WebSocket open and streams PCM continuously
  (an `AudioWorklet` on `getUserMedia`).
- The **sidecar** runs openWakeWord + Silero VAD on the stream at all
  times — both are tiny and CPU-cheap, which is what makes 24/7
  listening practical.
- **SLEEPING:** only a detected wake phrase promotes to AWAKE (UI shows
  "listening…", optionally a short spoken confirmation).
- **AWAKE:** VAD segments each utterance → whisper.cpp → transcript →
  auto-submit → answer → TTS. Idle timer (client or sidecar) drops
  AWAKE→SLEEPING after `VOICE_IDLE_TIMEOUT`.
- **Echo guard:** the mic is muted while TTS is playing, so the bot
  never hears its own answer (and never re-triggers on it).
- Barge-in (stopping TTS when the user starts talking) is a nice-to-have
  for Phase 3; the simple rule "TTS must finish first" is acceptable.

## Phased Implementation Plan

### Phase 0 — Spikes and benchmarks (1–2 days)

Goal: kill the two unknowns before writing integration code.

1. Stand up whisper.cpp + piper1-gpl in a scratch container; measure
   STT latency and TTS real-time factor **on the target hardware**
   (including the ASCEND machine if it is a candidate host).
2. A/B-test piper1-gpl `de_DE` vs `en_GB` voices and (optionally)
   Kokoro-82M `af_*` for English, on real CityDB answers.
3. Decide `base` vs `small` STT model from measured latency/accuracy on
   typical question lengths.

### Phase 1 — Sidecar + push-to-talk + voice output (3–5 days)

1. **Sidecar container** (1.5–2.5 days): uvicorn app with `/health`,
   `POST /tts` (sentence-streaming WAV), `POST /stt` (audio → text),
   Dockerfile with model weights, compose service + `VOICE_*` env keys.
2. **WebUI input** (1–1.5 days): mic button next to the text box in the
   global `js=` block — record via `AudioWorklet`/`MediaRecorder`, send
   to `/stt`, fill the text box, trigger the existing submit path.
   Feature-detect + degrade gracefully when the sidecar is off or the
   mic is unavailable.
3. **WebUI output** (0.5–1 day): on final answer, clean markdown (reuse
   existing helpers), `POST /tts` per sentence, play via `AudioContext`;
   mute toggle; mic muted while speaking.

Result: a working "talk to the bot" experience, manual per utterance.

### Phase 2 — Constant listening + idle timeout + wake word (3–5 days)

1. Persistent WebSocket streaming PCM to the sidecar (1 day).
2. openWakeWord + Silero VAD pipeline in the sidecar, wake-phrase
   detection in SLEEPING, utterance segmentation in AWAKE (1.5–2 days).
3. One-time **custom wake-phrase training** ("hello citybot") via
   openWakeWord's offline notebook (half a day, CPU); tune the detection
   threshold against real-environment audio.
4. State machine, idle timer, echo guard, UI status indicator
   (1–1.5 days).

Result: the full desired behavior — "Hello CityBot" after the bot went
quiet re-arms it; no typing needed at all.

### Phase 3 — Polish (ongoing / optional, ~1–2 days each item)

- Barge-in (stop TTS when the user speaks).
- Interim/partial transcript feedback while speaking.
- Kokoro-82M as an optional English voice (`VOICE_TTS_ENGINE= kokoro`).
- German community Kokoro voice experiment.
- HTTPS/reverse-proxy runbook for LAN access (see risks).

**Total to full feature: roughly 1.5–2.5 weeks** of focused work, with a
usable manual-voice version after ~1 week.

## Risks and Caveats

1. **Secure context for the microphone.** `getUserMedia` only works on
   `https://` or `localhost`. The fullstack stack is served on
   `http://<host>:7860`, so **LAN users need a reverse proxy with a
   certificate** (or an SSH port-forward to `localhost`). Same-machine
   use works as-is. This is the most likely "it doesn't work over the
   network" issue and needs a short runbook.
2. **Licenses.** piper1-gpl is **GPL-3.0** (fine for a self-contained
   sidecar; document it), Silero VAD is **CC BY-NC 4.0** (fine here, not
   for commercial redistribution), whisper.cpp / Kokoro / openWakeWord
   are permissive (MIT / Apache-2.0).
3. **Latency expectations.** Local LLM answers can take many seconds; the
   wake/attention window must stay open across answer generation so the
   user can speak immediately after it finishes.
4. **Wake-word reliability** is the main tuning risk (false triggers from
   background noise). Mitigations: conservative threshold, require the
   phrase from the wake engine (not the transcript) in SLEEPING, and the
   transcript-based fallback as a second opinion.
5. **Gradio re-renders** swap the chat element during streaming; the
   audio state machine must live in the global `js=` block and be
   resilient to DOM replacement — the existing scroll/observer code
   already demonstrates the pattern.
6. **Hardware fit.** The sidecar is CPU-only and modest (~1–2 GB RAM),
   but STT/TTS throughput must be benchmarked on the actual host (Phase 0)
   before model sizes are finalized.

## Scope and Non-Goals

- No voice cloning, no speaker identification, no multi-speaker
  diarization.
- No changes to the MCP server, the agent runtime, or answer generation.
- No per-user voice profiles; one shared voice service with a per-session
  voice/language choice.
- No push-to-server microphone streaming beyond the session's browser tab
  (the audio never leaves the deployment, but it does traverse the
  LAN to the sidecar — same trust boundary as the UI itself).
- No support for browsers without a secure-context microphone
  (documented as a requirement).
