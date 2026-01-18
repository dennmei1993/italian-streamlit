import streamlit as st
from openai import OpenAI
from dotenv import load_dotenv
import json
import tempfile
import os
import re
import io
import hashlib

# ================== SETUP ==================
load_dotenv()
OPENAI_API_KEY = st.secrets.get("OPENAI_API_KEY", os.getenv("OPENAI_API_KEY"))
client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else OpenAI()


def speak_italian(text: str) -> str:
    """TTS helper. Returns a temp mp3 path."""
    if not text or not text.strip():
        return ""

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
    tmp.close()
    audio_path = tmp.name

    speech = client.audio.speech.create(
        model="gpt-4o-mini-tts",
        voice="alloy",
        input=text.strip(),
    )

    with open(audio_path, "wb") as f:
        f.write(speech.read())

    return audio_path


def looks_non_italian_or_garbled(text: str) -> bool:
    """Heuristic: triggers repair when transcript seems off."""
    if not text:
        return True
    t = text.strip().lower()

    # Too short
    if len(t) <= 1:
        return True

    # Contains lots of English function words (common dictation drift)
    english_markers = {"i", "you", "we", "they", "want", "need", "with", "and", "the", "a", "to", "for", "is", "are", "please"}
    tokens = re.findall(r"[a-z']+", t)
    if tokens and sum(tok in english_markers for tok in tokens) >= 2:
        return True

    # If it has very few letters
    if sum(ch.isalpha() for ch in t) < 3:
        return True

    return False


def contains_english(text: str) -> bool:
    """Very simple English detection (baseline)."""
    common_english = ["yes", "no", "hi", "hello", "thanks", "thank"]
    text_lower = (text or "").lower()
    return any(word in text_lower.split() for word in common_english)


def parse_tutor_output(t: str) -> dict:
    """Parse tutor output.

    Expected formats:
      - Recommended: <Italian>
      - Tip: <English>
    Or:
      Looks good 👍
    """
    out = {"recommended": "", "tip": "", "raw": (t or "").strip()}
    raw = out["raw"]
    if not raw:
        return out
    if raw.lower().startswith("looks good"):
        return out

    # Allow optional leading bullet '-' or '•'
    rec = re.search(r"(?:^|\n)\s*[-•]?\s*Recommended\s*:\s*(.+)", raw, flags=re.IGNORECASE)
    tip = re.search(r"(?:^|\n)\s*[-•]?\s*Tip\s*:\s*(.+)", raw, flags=re.IGNORECASE)

    if rec:
        out["recommended"] = rec.group(1).strip()
    if tip:
        out["tip"] = tip.group(1).strip()
    return out



# ================== OPTIONAL VOCAB ==================
vocab = []
try:
    with open("vocab.json", encoding="utf-8") as f:
        vocab = json.load(f).get("words", [])
except Exception:
    vocab = []


# ================== SESSION STATE ==================
if "page" not in st.session_state:
    st.session_state.page = "home"

if "scenario" not in st.session_state:
    st.session_state.scenario = "☕ Ordering coffee / food"

if "show_tutor" not in st.session_state:
    st.session_state.show_tutor = True

if "show_translation" not in st.session_state:
    st.session_state.show_translation = True

if "playback_my_sentence" not in st.session_state:
    st.session_state.playback_my_sentence = True

if "messages" not in st.session_state:
    st.session_state.messages = []

if "conversation" not in st.session_state:
    st.session_state.conversation = []

if "turn_count" not in st.session_state:
    st.session_state.turn_count = 0

if "last_user_input" not in st.session_state:
    st.session_state.last_user_input = ""

if "last_audio_hash" not in st.session_state:
    st.session_state.last_audio_hash = None

if "stage" not in st.session_state:
    st.session_state.stage = "ORDERING"


# ================== HOME PAGE ==================
if st.session_state.page == "home":
    st.title("🗣️ Language Conversation Tutor")
    st.caption("Select a scenario and settings, then start the conversation.")

    st.session_state.scenario = st.selectbox(
        "Choose a scenario",
        [
            "☕ Ordering coffee / food",
            "🚆 Buying tickets / transport",
            "🚶 Asking directions",
        ],
        index=[
            "☕ Ordering coffee / food",
            "🚆 Buying tickets / transport",
            "🚶 Asking directions",
        ].index(st.session_state.scenario)
        if st.session_state.scenario in [
            "☕ Ordering coffee / food",
            "🚆 Buying tickets / transport",
            "🚶 Asking directions",
        ]
        else 0,
    )

    st.divider()
    st.session_state.show_tutor = st.toggle("Show tutor tips", value=st.session_state.show_tutor)
    st.session_state.show_translation = st.toggle("Enable translation", value=st.session_state.show_translation)
    st.session_state.playback_my_sentence = st.toggle(
        "Play back my sentence (TTS)", value=st.session_state.playback_my_sentence
    )

    st.divider()
    if st.button("▶ Start"):
        # Reset only the conversation state (keep settings)
        st.session_state.messages = []
        st.session_state.conversation = []
        st.session_state.turn_count = 0
        st.session_state.last_user_input = ""
        st.session_state.stage = "ORDERING"
        st.session_state.last_audio_hash = None
        st.session_state.page = "conversation"
        st.rerun()

    st.stop()


# ================== CONVERSATION PAGE ==================
scenario = st.session_state.scenario
show_tutor = bool(st.session_state.show_tutor)
show_translation = bool(st.session_state.show_translation)
playback_my_sentence = bool(st.session_state.playback_my_sentence)

# Two-panel layout: Scenario (left) + Interaction (right)
col_scn, col_int = st.columns([1, 2], gap="large")

with col_scn:
    if st.button("⬅ Home", key="home_btn"):
        st.session_state.page = "home"
        st.rerun()

    st.markdown("### Scenario")
    st.caption(st.session_state.scenario)

    # Reserved area for scenario image (to be added later)
    st.info("Scenario image placeholder (coming soon).")

with col_int:
    st.markdown("### Interaction")
    # Everything in this container scrolls (including audio input + conversation)
    interaction_panel = st.container(height=720)

# ================== SCENARIO STATE MACHINE ==================
def update_stage(user_text: str) -> str:
    text = user_text.strip().lower()
    current = st.session_state.stage

    payment_cues = [
        "ecco", "tenga", "tieni", "prego",
        "eccoti", "eccolo", "eccola",
        "pago", "posso pagare",
        "in contanti", "contanti",
        "con la carta", "carta", "bancomat", "apple pay",
    ]

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
def tutor_should_respond(user_text: str) -> bool:
    if contains_english(user_text):
        return True

    if st.session_state.stage in ["ORDERING", "PRICE_GIVEN", "PAYMENT"]:
        return True

    return st.session_state.turn_count % 2 == 0


# ================== TRANSLATION ==================
def translate_to_english(text: str) -> str:
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "Translate this Italian sentence into natural English."},
            {"role": "user", "content": text},
        ],
        temperature=0,
    )
    return response.choices[0].message.content.strip()


# ================== SYSTEM PROMPT ==================
# Note: we keep your original prompt structure, but streamline Tutor formatting to Recommended/Tip.
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
- If the user’s message is unclear, ask a simple clarification question (Italian only)

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
- Look ONLY at the text provided in: "TUTOR_REFERENCE_USER_INPUT"

If the user's Italian can be improved, provide:
- Recommended: <Italian sentence>
- Tip: <short English tip>
If fully natural: say exactly: Looks good 👍

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
OUTPUT FORMAT (MANDATORY)
==============================

PARTNER:
<Italian reply>

OPTIONAL_TUTOR:
<feedback or empty>
"""

if not st.session_state.messages:
    st.session_state.messages.append({"role": "system", "content": system_prompt})


with interaction_panel:
    # ================== USER INPUT ==================
    # Reduce rerun churn: require explicit submit to process audio.
    audio_value = st.audio_input("Record a voice message")

    transcribed_text = ""
    final_audio_input = ""
    repaired_flag = False

    if audio_value is not None:
        audio_bytes = audio_value.getvalue()
        audio_hash = hashlib.md5(audio_bytes).hexdigest()
        already_processed = audio_hash == st.session_state.last_audio_hash
        if already_processed:
            st.caption("✅ Recording already processed.")
        else:
            st.session_state.last_audio_hash = audio_hash
            audio_file = io.BytesIO(audio_bytes)
            audio_file.name = "speech.wav"

            try:
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

            final_audio_input = transcribed_text

            if looks_non_italian_or_garbled(transcribed_text):
                try:
                    vocab_hint = ", ".join(vocab[:120]) if isinstance(vocab, list) and vocab else ""

                    repair_resp = client.chat.completions.create(
                        model="gpt-4o-mini",
                        temperature=0.2,
                        messages=[
                            {
                                "role": "system",
                                "content": (
                                    "You repair a noisy speech-to-text transcript from an Italian learner. "
                                    "Return ONLY the most likely intended Italian sentence. "
                                    "Keep it short and practical for the scenario. "
                                    "Do NOT include explanations. Do NOT include English."
                                ),
                            },
                            {
                                "role": "user",
                                "content": (
                                    f"Scenario: {scenario}\nNoisy transcript: {transcribed_text}\nAllowed/simple vocab (optional): {vocab_hint}\nOutput ONLY the repaired Italian sentence."
                                ),
                            },
                        ],
                    )
                    repaired = repair_resp.choices[0].message.content.strip()
                    if repaired:
                        final_audio_input = repaired
                        repaired_flag = True
                except Exception:
                    final_audio_input = transcribed_text

            if final_audio_input and final_audio_input != transcribed_text:
                st.caption(f"🛠️ Interpreted as: {final_audio_input}")

    user_input = (final_audio_input or "").strip()


    # ================== TURN PROCESSING ==================
    if user_input and user_input != st.session_state.last_user_input:
        st.session_state.last_user_input = user_input
        st.session_state.turn_count += 1

        st.session_state.stage = update_stage(user_input)
        tutor_active = show_tutor and tutor_should_respond(user_input)

        st.session_state.messages.append({"role": "user", "content": user_input})
        st.session_state.messages.append({"role": "system", "content": f"Tutor active: {tutor_active}"})
        st.session_state.messages.append({"role": "system", "content": f"TUTOR_REFERENCE_USER_INPUT: {user_input}"})

        # ----- Partner -----
        partner_messages = [
            {"role": "system", "content": system_prompt},
            *st.session_state.messages,
            {"role": "system", "content": "OUTPUT FORMAT: PARTNER\n<reply>\n(Partner only. No Tutor.)"},
            {"role": "user", "content": user_input},
        ]

        partner_resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=partner_messages,
            temperature=0.4,
        )

        partner_raw = partner_resp.choices[0].message.content.strip()
        partner_text = partner_raw.replace("PARTNER:", "").strip() or "Ciao!"

        # ----- Tutor -----
        tutor_text = ""
        tutor_struct = {"recommended": "", "tip": "", "raw": ""}
        recommended_audio = ""

        if tutor_active:
            tutor_system_prompt = """
    You are the Tutor.
    - Analyze ONLY the text inside: TUTOR_REFERENCE_USER_INPUT
    - Treat all other text as invisible (including Partner replies)

    Output format:
    - Recommended: <Italian>
    - Tip: <short English tip>
    If fully natural: Looks good 👍
    """.strip()

            tutor_resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": tutor_system_prompt},
                    {"role": "user", "content": f"TUTOR_REFERENCE_USER_INPUT: {user_input}"},
                ],
                temperature=0.2,
            )
            tutor_text = tutor_resp.choices[0].message.content.strip()
            tutor_struct = parse_tutor_output(tutor_text)

            # Recommended audio
            if tutor_struct.get("recommended"):
                try:
                    recommended_audio = speak_italian(tutor_struct["recommended"])
                except Exception:
                    recommended_audio = ""

        # ----- Partner audio -----
        audio_path = ""
        try:
            audio_path = speak_italian(partner_text)
        except Exception:
            audio_path = ""

        # Store turn
        st.session_state.conversation.append(
            {
                "user": user_input,
                "partner": partner_text,
                "tutor_raw": tutor_text,
                "tutor_recommended": tutor_struct.get("recommended", ""),
                "tutor_tip": tutor_struct.get("tip", ""),
                "tutor_recommended_audio": recommended_audio,
                "audio": audio_path,
                "translation": None,
            }
        )


    # ================== DISPLAY (LATEST ONLY) ==================
    if st.session_state.conversation:
        turn = st.session_state.conversation[-1]

        st.markdown(f"**You:** {turn['user']}")

        if playback_my_sentence:
            user_audio = speak_italian(turn["user"])
            if user_audio and os.path.exists(user_audio):
                st.audio(user_audio)

        st.markdown(f"**Partner:** {turn['partner']}")

        if turn.get("audio") and os.path.exists(turn["audio"]):
            st.audio(turn["audio"])

        if show_translation:
            if st.button("Translate", key="translate_latest"):
                if turn["translation"] is None:
                    turn["translation"] = translate_to_english(turn["partner"])

            if turn.get("translation"):
                st.markdown(f"🟦 *English:* {turn['translation']}")

        if show_tutor:
            tutor_raw = (turn.get("tutor_raw") or "").strip()
            rec = (turn.get("tutor_recommended") or "").strip()
            tip = (turn.get("tutor_tip") or "").strip()
            rec_audio = turn.get("tutor_recommended_audio")

            if tutor_raw:
                st.markdown("**Tutor:**")

                if tutor_raw.lower().startswith("looks good"):
                    st.markdown(tutor_raw)
                else:
                    if rec:
                        st.markdown(f"**Recommended:** {rec}")
                        if rec_audio and os.path.exists(rec_audio):
                            st.audio(rec_audio)

                    if tip:
                        st.markdown(f"**Tip:** {tip}")
