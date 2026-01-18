import io
import json
import os
import re
import tempfile
import time
import wave

import streamlit as st
from openai import OpenAI
from dotenv import load_dotenv

# Optional (recommended): auto-stop recording after silence via streamlit-webrtc
try:
    import numpy as np  # type: ignore
    from streamlit_webrtc import (  # type: ignore
        ClientSettings,
        WebRtcMode,
        webrtc_streamer,
    )
    _WEBRTC_AVAILABLE = True
except Exception:
    _WEBRTC_AVAILABLE = False

OPENAI_API_KEY = st.secrets.get("OPENAI_API_KEY", os.getenv("OPENAI_API_KEY"))


def speak_italian(text: str) -> str:
    if not text or not text.strip():
        return ""

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
    tmp.close()
    audio_path = tmp.name

    speech = client.audio.speech.create(
        model="gpt-4o-mini-tts",
        voice="alloy",
        input=text.strip()
    )

    with open(audio_path, "wb") as f:
        f.write(speech.read())

    return audio_path


def looks_non_italian_or_garbled(text: str) -> bool:
    """Heuristic: triggers repair when transcript seems off."""
    if not text:
        return True
    t = text.strip().lower()

    # Too short and not useful
    if len(t) <= 1:
        return True

    # Contains lots of English function words (common dictation drift)
    english_markers = {"i","you","we","they","want","need","with","and","the","a","to","for","is","are","please"}
    tokens = re.findall(r"[a-z']+", t)
    if tokens and sum(tok in english_markers for tok in tokens) >= 2:
        return True

    # If it has very few Italian-looking characters and lots of random symbols
    if sum(ch.isalpha() for ch in t) < 3:
        return True

    return False


def _audio_frames_to_wav_bytes(frames: list, sample_rate: int) -> bytes:
    """Convert streamlit-webrtc AudioFrames to WAV bytes (mono, 16-bit)."""
    if not frames or not sample_rate:
        return b""

    pcm_parts: list[bytes] = []
    for fr in frames:
        arr = fr.to_ndarray()
        # arr shape is usually (channels, samples)
        if arr.ndim == 2:
            mono = arr[0]
        else:
            mono = arr
        # Ensure int16 PCM
        if mono.dtype != np.int16:
            mono = mono.astype(np.int16)
        pcm_parts.append(mono.tobytes())

    pcm = b"".join(pcm_parts)
    if not pcm:
        return b""

    buff = io.BytesIO()
    with wave.open(buff, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # int16
        wf.setframerate(int(sample_rate))
        wf.writeframes(pcm)
    return buff.getvalue()


def _frame_rms_int16_mono(frame) -> float:
    """RMS energy for a webrtc audio frame."""
    arr = frame.to_ndarray()
    if arr.ndim == 2:
        mono = arr[0]
    else:
        mono = arr
    if mono.dtype != np.int16:
        mono = mono.astype(np.int16)
    # RMS
    return float(np.sqrt(np.mean(mono.astype(np.float32) ** 2)))


# ================== SETUP ==================
load_dotenv()
client = OpenAI()

with open("vocab.json", encoding="utf-8") as f:
    vocab = json.load(f)["words"]

# st.title("Italian Conversation Practice 🇮🇹")
# st.write("Partner speaks Italian. Tutor helps when needed.")

scenario = st.selectbox(
    "Choose a scenario",
    [
        "☕ Ordering coffee / food",
        "🚆 Buying tickets / transport",
        "🚶 Asking directions"
    ]
)

# ================  Make English detection explicit =============
def contains_english(text: str) -> bool:
    common_english = ["yes", "no", "hi", "hello", "thanks", "thank"]
    text_lower = text.lower()
    return any(word in text_lower.split() for word in common_english)


# ================== SESSION STATE ==================
if "messages" not in st.session_state:
    st.session_state.messages = []

if "conversation" not in st.session_state:
    st.session_state.conversation = []

if "turn_count" not in st.session_state:
    st.session_state.turn_count = 0

if "last_user_input" not in st.session_state:
    st.session_state.last_user_input = ""

if "stage" not in st.session_state:
    st.session_state.stage = "ORDERING"

# ================== SCENARIO STATE MACHINE ==================
def update_stage(user_text: str) -> str:
    text = user_text.strip().lower()
    current = st.session_state.stage

    payment_cues = [
        "ecco", "tenga", "tieni", "prego",
        "eccoti", "eccolo", "eccola",
        "pago", "posso pagare",
        "in contanti", "contanti",
        "con la carta", "carta", "bancomat", "apple pay"
    ]

    # Only treat as payment handover when a price was just given
    if current == "PRICE_GIVEN" and any(cue in text for cue in payment_cues):
        return "PAYMENT"

    if current == "PRICE_GIVEN" and text in ["ok", "va bene", "bene", "sì", "si"]:
        return "PAYMENT"

    if any(x in text for x in ["quanto", "how much"]):
        return "PRICE_GIVEN"

    if text in ["si", "sì", "ok", "va bene"]:
        return "PAYMENT"

    if any(x in text for x in ["grazie", "thanks"]):
        return "CLOSING"

    return st.session_state.stage


# ================== TUTOR TRIGGER ==================
def tutor_should_respond(user_text):
    # English → ALWAYS tutor
    if contains_english(user_text):
        return True

    # Italian mistake → ALWAYS tutor
    if st.session_state.stage in ["ORDERING", "PRICE_GIVEN", "PAYMENT"]:
        return True

    # Otherwise, optional praise every 2 turns
    return st.session_state.turn_count % 2 == 0



# ================== TRANSLATION ==================
def translate_to_english(text: str) -> str:
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "Translate this Italian sentence into natural English."},
            {"role": "user", "content": text}
        ],
        temperature=0
    )
    return response.choices[0].message.content.strip()


# ================== SYSTEM PROMPT ==================
system_prompt = f"""
You are an Italian language assistant playing TWO roles.

GREETING RULE:
- If the user greets (hi, hello, ciao):
  Partner replies with a simple Italian greeting (e.g. "Ciao!")

==============================
ROLE 1 — Conversation Partner
==============================
- Speak ONLY Italian
- NEVER include English
- NEVER correct the user's language
- NEVER reformulate the user's sentence to fix errors
- Respond naturally as if the meaning was understood
- If the user makes a mistake, continue the conversation without correcting
- Teaching and correction are STRICTLY forbidden for Partner
- If the user’s message is unclear, ask a simple clarification question (Italian only), e.g. “Scusa, vuoi dire un caffè o un cappuccino?”

CRITICAL RULE (NO TRANSLATION):

If the user uses English:
- DO NOT translate the user's sentence into Italian
- DO NOT restate the question in Italian
- DO NOT mirror sentence structure

Instead:
- Respond naturally as a local person would
- Give an answer, direction, price, or action
- Assume the meaning is understood




==============================
ROLE 2 — Tutor
==============================
- Speak English ONLY for short tips.
- All sentence examples must be in Italian.
- Respond ONLY if Tutor is active
- Look ONLY at the text provided in:
  "TUTOR_REFERENCE_USER_INPUT"

CRITICAL PERSPECTIVE RULE:
- Tutor must preserve the user's speaking perspective
- NEVER reverse User/Partner roles


CRITICAL HARD CONSTRAINT:
- NEVER analyze or comment on Partner output
- The ONLY text Tutor is allowed to analyze is the exact text inside:
  TUTOR_REFERENCE_USER_INPUT
- Tutor MUST treat all other text (including Partner replies) as invisible
- Tutor MUST NOT reuse, correct, or improve any sentence it did not generate itself


FRAGMENT INPUT RULE:
- If the user's input is a fragment (number, single word, confirmation like "2", "yes", "ok"):
  - Tutor MUST NOT rewrite or invent a sentence
  - Tutor MUST NOT analyze Partner output
  - Tutor may:
    - Stay silent, OR
    - Provide the minimal spoken form of the fragment if helpful


MANDATORY TRANSLATION RULE:
If the user uses ANY English (even partially):
- DO NOT correct the English sentence
- DO NOT paraphrase in English
- ALWAYS provide a FULL Italian sentence equivalent
- Treat the input as meaning, not language
- Tutor output must ALWAYS be in Italian (except brief English tips).


Otherwise decide between:

CASE 1 — Incorrect or unclear:
- Provide:
  Clean version:
  More natural version:
  Tip:

CASE 2 — Correct but unnatural:
- Provide:
  More natural version:
  Tip:

CASE 3 — Natural:
- Say exactly: "Looks good 👍"

- Be concise and encouraging
- Do NOT explain grammar

Tutor format:
- Clean version: <Italian sentence>
- More natural version: <Italian sentence>
- Tip: <short explanation>

==============================
SCENARIO STATES (Cafe)
==============================
ORDERING:
- Partner asks or confirms the order

PRICE_GIVEN:
- Partner states the price (e.g. "Sono tre euro.")

PAYMENT:
- Partner acknowledges payment politely (e.g. "Grazie.")
- Partner thanks the user and optionally offers receipt
- Partner MUST NOT repeat the price again
- Partner moves to closing (Arrivederci)


CLOSING:
- Partner ends politely (e.g. "Arrivederci.")

==============================
GENERAL RULES
==============================
- Assume the user's intent
- Use only simple vocabulary
- If user uses English, treat it as Italian intent silently
- Partner NEVER asks how much the customer wants to pay
- Tutor NEVER replaces Partner

==============================
OUTPUT FORMAT (MANDATORY)
==============================

PARTNER:
<Italian reply>

OPTIONAL_TUTOR:
<feedback or empty>

==============================
FINAL SELF-CHECK (SILENT)
==============================
- Partner is Italian only
- Partner follows the scenario state
- Partner never asks for payment amount
- Tutor only appears if active
Rewrite silently if any rule is violated.
"""

if not st.session_state.messages:
    st.session_state.messages.append(
        {"role": "system", "content": system_prompt}
    )

# ================== USER INPUT ==================

import io

st.subheader("🎙️ Speak")

transcribed_text = ""
final_audio_input = ""  # what we will actually send into your app flow
repaired_flag = False

# ------- Option A: streamlit-webrtc (auto-stop after silence) -------
use_auto_stop = st.toggle(
    "Auto-stop after 2s of silence (recommended)",
    value=True if _WEBRTC_AVAILABLE else False,
    disabled=not _WEBRTC_AVAILABLE,
)
if not _WEBRTC_AVAILABLE:
    st.caption("To enable auto-stop, install: streamlit-webrtc and numpy")

captured_wav_bytes: bytes = b""
sample_rate: int = 0

if use_auto_stop and _WEBRTC_AVAILABLE:
    # Initialize recorder state
    if "rec_frames" not in st.session_state:
        st.session_state.rec_frames = []
    if "rec_sample_rate" not in st.session_state:
        st.session_state.rec_sample_rate = 0
    if "rec_speech_started" not in st.session_state:
        st.session_state.rec_speech_started = False
    if "rec_last_voice_ts" not in st.session_state:
        st.session_state.rec_last_voice_ts = 0.0
    if "rec_done" not in st.session_state:
        st.session_state.rec_done = False
    if "rec_wav_bytes" not in st.session_state:
        st.session_state.rec_wav_bytes = b""

    col_rec_a, col_rec_b = st.columns([1, 1])
    with col_rec_a:
        clear_rec = st.button("Clear recording")
    with col_rec_b:
        rms_threshold = st.slider("Voice sensitivity", 100, 2000, 600, step=50)

    if clear_rec:
        st.session_state.rec_frames = []
        st.session_state.rec_sample_rate = 0
        st.session_state.rec_speech_started = False
        st.session_state.rec_last_voice_ts = 0.0
        st.session_state.rec_done = False
        st.session_state.rec_wav_bytes = b""
        st.rerun()

    ctx = webrtc_streamer(
        key="mic",
        mode=WebRtcMode.SENDONLY,
        client_settings=ClientSettings(media_stream_constraints={"audio": True, "video": False}),
        audio_receiver_size=1024,
    )

    if ctx.audio_receiver and not st.session_state.rec_done:
        # Drain any available frames quickly (do not block long)
        try:
            frames = ctx.audio_receiver.get_frames(timeout=0.1)
        except Exception:
            frames = []

        now = time.time()
        for fr in frames:
            # st-webrtc frames provide sample_rate
            if not st.session_state.rec_sample_rate:
                try:
                    st.session_state.rec_sample_rate = int(fr.sample_rate)
                except Exception:
                    st.session_state.rec_sample_rate = 16000

            rms = _frame_rms_int16_mono(fr)
            if rms >= float(rms_threshold):
                st.session_state.rec_speech_started = True
                st.session_state.rec_last_voice_ts = now

            # Buffer frames once we have started speech (keeps file short)
            if st.session_state.rec_speech_started:
                st.session_state.rec_frames.append(fr)

        # Auto-stop after 2 seconds of silence once speech has started
        if st.session_state.rec_speech_started and st.session_state.rec_last_voice_ts:
            if now - float(st.session_state.rec_last_voice_ts) >= 2.0:
                wav_bytes = _audio_frames_to_wav_bytes(
                    st.session_state.rec_frames,
                    st.session_state.rec_sample_rate,
                )
                st.session_state.rec_wav_bytes = wav_bytes
                st.session_state.rec_done = True

    if st.session_state.rec_done and st.session_state.rec_wav_bytes:
        st.success("Captured audio (auto-stopped after silence).")
        captured_wav_bytes = st.session_state.rec_wav_bytes
        sample_rate = st.session_state.rec_sample_rate
        st.audio(captured_wav_bytes, format="audio/wav")
else:
    # ------- Fallback: Streamlit audio_input (manual stop) -------
    audio_value = st.audio_input("Record a voice message")
    if audio_value is not None:
        captured_wav_bytes = audio_value.getvalue()


if captured_wav_bytes:
    audio_file = io.BytesIO(captured_wav_bytes)
    audio_file.name = "speech.wav"

    try:
        # 1) Primary transcription (tolerant + no translation)
        tr = client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file,
            prompt=(
                "Italian language. The speaker is a learner with imperfect pronunciation. "
                "Transcribe exactly as spoken. If unsure, choose the closest Italian words "
                "that fit a real-life conversation. Do not translate."
            ),
            response_format="text",
            temperature=0,
        )
        transcribed_text = (tr if isinstance(tr, str) else getattr(tr, "text", "")).strip()
    except Exception as e:
        st.warning(f"Audio transcription failed: {e}")
        transcribed_text = ""

    # Default: use what we heard
    final_audio_input = transcribed_text

    # 2) Automatic repair fallback (Mode A)
    if looks_non_italian_or_garbled(transcribed_text):
        try:
            vocab_hint = ""
            try:
                if "vocab" in globals() and isinstance(vocab, list) and vocab:
                    vocab_hint = ", ".join(vocab[:120])
            except Exception:
                pass

            repair_resp = client.chat.completions.create(
                model="gpt-4o-mini",
                temperature=0.2,
                messages=[
                    {"role": "system", "content": (
                        "You repair a noisy speech-to-text transcript from an Italian learner. "
                        "Return ONLY the most likely intended Italian sentence. "
                        "Keep it short and practical for the scenario. "
                        "Do NOT include explanations. Do NOT include English."
                    )},
                    {"role": "user", "content": (
                        f"Scenario: {scenario}\n"
                        f"Noisy transcript: {transcribed_text}\n"
                        f"Allowed/simple vocab (optional): {vocab_hint}\n"
                        "Output ONLY the repaired Italian sentence."
                    )},
                ],
            )
            repaired = repair_resp.choices[0].message.content.strip()
            if repaired:
                final_audio_input = repaired
        except Exception:
            final_audio_input = transcribed_text

    # Optional debug while testing
    # if transcribed_text:
      #  st.caption(f"🎧 Heard: {transcribed_text}")
    if final_audio_input and final_audio_input != transcribed_text:
        st.caption(f"🛠️ Interpreted as: {final_audio_input}")

# typed_input = st.text_input("You:")
user_input = final_audio_input.strip() # if final_audio_input.strip() else typed_input.strip()


if user_input and user_input != st.session_state.last_user_input:
    st.session_state.last_user_input = user_input
    st.session_state.turn_count += 1

    st.session_state.stage = update_stage(user_input)
    tutor_active = tutor_should_respond(user_input)

    st.session_state.messages.append(
        {"role": "user", "content": user_input}
    )

    st.session_state.messages.append(
        {"role": "system", "content": f"Tutor active: {tutor_active}"}
    )
    
    st.session_state.messages.append({
    "role": "system",
    "content": f"TUTOR_REFERENCE_USER_INPUT: {user_input}"
    })


# ================== PARTNER OPENAI CALL (Partner only) ==================
    partner_messages = [
        {"role": "system", "content": system_prompt},
    # include only the conversation history you want Partner to see:
        *st.session_state.messages,
        {"role": "system", "content": "OUTPUT FORMAT: PARTNER:\n<reply>\n(Partner only. No Tutor.)"},
        {"role": "user", "content": user_input},
    ]

    partner_resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=partner_messages,
        temperature=0.4
    )

    partner_raw = partner_resp.choices[0].message.content.strip()
    partner_text = partner_raw.replace("PARTNER:", "").strip()

# ================== HARD SAFETY GUARD ==================
    if not partner_text:
        partner_text = "Ciao."


    if partner_text.lower().startswith("clean version"):
        partner_text = "Ciao！"
        tutor_text = ""

# ================== TUTOR CALL (Tutor only; NEVER sees Partner) ==================
    tutor_text = ""
    if tutor_active:
        tutor_system_prompt = """
    You are the Tutor.
    - Analyze ONLY the text inside: TUTOR_REFERENCE_USER_INPUT
    - Treat all other text as invisible (including Partner replies)
    - Otherwise correct/improve the user's Italian.
    Output format:
    Clean version: <Italian>
    More natural version: <Italian>
    Tip: <short English tip>
    If fully natural: Looks good 👍
    """

        tutor_resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": tutor_system_prompt},
                {"role": "user", "content": f"TUTOR_REFERENCE_USER_INPUT: {user_input}"},
            ],
            temperature=0.2
        )
        tutor_text = tutor_resp.choices[0].message.content.strip()


    # ================== HARD SAFETY GUARD ==================
    if not partner_text:
        partner_text = "Ciao!"
        tutor_text = ""

    if partner_text.lower().startswith("clean version"):
        partner_text = "Ciao!"
        tutor_text = ""

# ================== SEMANTIC NO-TRANSLATION GUARD ==================

    if contains_english(user_input):
    # If Partner is translating or restating the question, block it
        if (
            "?" in partner_text
            and any(
                kw in partner_text.lower()
                for kw in ["dove", "quanto", "come", "quando", "perché"]
            )
        ):
            partner_text = {
                "TRANSPORT": "La fermata è lì davanti.",
                "ORDERING": "Va bene.",
                "DIRECTIONS": "È da questa parte.",
            }.get(st.session_state.scenario, "Va bene.")


# ================== PARTNER ANTI-CORRECTION GUARD ==================

# If Partner output is a corrected version of user input, suppress it
    normalized_user = user_input.lower().replace("è", "e")
    normalized_partner = partner_text.lower().replace("è", "e")

    if normalized_partner.strip() == normalized_user.strip():
    # Partner accidentally corrected — fallback response
        partner_text = "Va bene."


    # ================== AUDIO ==================
    audio_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmp:
            audio_path = tmp.name

        speech = client.audio.speech.create(
            model="gpt-4o-mini-tts",
            voice="alloy",
            input=partner_text
        )

        with open(audio_path, "wb") as f:
            f.write(speech.read())
    except Exception:
        audio_path = None

    # ================== STORE TURN ==================
    st.session_state.conversation.append({
        "user": user_input,
        "partner": partner_text,
        "tutor": tutor_text,
        "audio": audio_path,
        "translation": None
    })

# ================== DISPLAY (LATEST ONLY) ==================
# st.subheader("Latest turn")

if st.session_state.conversation:
    i = len(st.session_state.conversation) - 1
    turn = st.session_state.conversation[-1]

    st.markdown(f"**You:** {turn['user']}")

    #if st.button("🔊 Listen (Italian pronunciation)", key=f"speak_user_{i}"):
    user_audio = speak_italian(turn["user"])
    if user_audio and os.path.exists(user_audio):
        st.audio(user_audio)

    st.markdown(f"**Partner:** {turn['partner']}")

    if turn["audio"] and os.path.exists(turn["audio"]):
        st.audio(turn["audio"])

    if st.button("Transalte", key="translate_latest"):
        if turn["translation"] is None:
            turn["translation"] = translate_to_english(turn["partner"])

    if turn["translation"]:
        st.markdown(f"🟦 *English:* {turn['translation']}")

    if turn["tutor"]:
        st.markdown("**Tutor:**")
        st.markdown(turn["tutor"])
# else:
    # st.write("Say something to start.")


# ================== RESET ==================
# if st.button("Reset Conversation"):
  #  for turn in st.session_state.conversation:
   #     if turn["audio"] and os.path.exists(turn["audio"]):
    #        os.remove(turn["audio"])
    #st.session_state.clear()
    #st.stop()
