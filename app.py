import io
import json
import os
import re
import tempfile

import streamlit as st
from dotenv import load_dotenv
from openai import OpenAI

# ================== CONFIG / SETUP ==================
load_dotenv()

OPENAI_API_KEY = st.secrets.get("OPENAI_API_KEY", os.getenv("OPENAI_API_KEY"))
client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else OpenAI()

st.set_page_config(page_title="Language Conversation Tutor", page_icon="🗣️")

# ================== OPTIONAL VOCAB LOAD ==================
vocab_words: list[str] = []
try:
    with open("vocab.json", encoding="utf-8") as f:
        vocab_words = json.load(f).get("words", []) or []
except Exception:
    vocab_words = []

# ================== SCENARIOS (V1.1) ==================
SCENARIOS = {
    "cafe": {
        "label": "☕ Ordering coffee / food",
        "initial_stage": "ORDERING",
        "stages": ["ORDERING", "PRICE_GIVEN", "PAYMENT", "CLOSING"],
        "partner_prompt": """
You are a friendly local conversation partner in an Italian cafe.

Rules:
- Speak ONLY Italian.
- Do NOT teach. Do NOT correct the user.
- If the user uses English, do NOT translate or restate their sentence; just respond naturally as if you understood.
- Keep replies short and practical.

Scenario stages:
- ORDERING: ask/confirm the order.
- PRICE_GIVEN: state the price (e.g., \"Sono tre euro.\").
- PAYMENT: acknowledge payment politely; do NOT repeat the price; optionally offer receipt.
- CLOSING: end politely.
""".strip(),
    },
    "transport": {
        "label": "🚆 Buying tickets / transport",
        "initial_stage": "ASKING_ROUTE",
        "stages": ["ASKING_ROUTE", "TICKET", "CONFIRMATION", "CLOSING"],
        "partner_prompt": """
You are a helpful local at a train/metro station in Italy.

Rules:
- Speak ONLY Italian.
- Do NOT teach. Do NOT correct the user.
- If the user uses English, do NOT translate or restate their sentence; just respond naturally as if you understood.
- Keep replies short and practical.

Scenario stages:
- ASKING_ROUTE: help with destination/platform/which line.
- TICKET: help with buying/validating ticket.
- CONFIRMATION: confirm details (line, platform, direction, time).
- CLOSING: end politely.
""".strip(),
    },
    "directions": {
        "label": "🚶 Asking directions",
        "initial_stage": "ASKING",
        "stages": ["ASKING", "CLARIFYING", "CONFIRMING", "CLOSING"],
        "partner_prompt": """
You are a helpful local giving directions in an Italian city.

Rules:
- Speak ONLY Italian.
- Do NOT teach. Do NOT correct the user.
- If the user uses English, do NOT translate or restate their sentence; just respond naturally as if you understood.
- Keep replies short and practical.

Scenario stages:
- ASKING: ask where they want to go / respond to request.
- CLARIFYING: ask one simple clarification if needed.
- CONFIRMING: confirm route/landmarks.
- CLOSING: end politely.
""".strip(),
    },
}

# ================== HELPERS ==================

ENGLISH_HINT_WORDS = {
    "the", "and", "is", "to", "for", "with", "want", "where", "how", "please",
    "i", "you", "we", "they", "this", "that", "in", "on", "at", "from",
    "can", "could", "would", "do", "does", "did", "thanks", "thank", "hello", "hi",
}


def is_likely_english(text: str) -> bool:
    if not text:
        return False
    tokens = re.findall(r"[a-z']+", text.lower())
    if not tokens:
        return False
    hits = sum(tok in ENGLISH_HINT_WORDS for tok in tokens)
    # ratio + minimum hits to avoid false positives
    return hits >= 2 or (hits >= 1 and hits / max(1, len(tokens)) >= 0.25)


def looks_non_target_or_garbled(text: str) -> bool:
    """Simple heuristic: triggers repair when transcript seems off."""
    if not text:
        return True
    t = text.strip().lower()
    if len(t) <= 1:
        return True
    # too many English markers usually means dictation drift
    tokens = re.findall(r"[a-z']+", t)
    if tokens and sum(tok in ENGLISH_HINT_WORDS for tok in tokens) >= 2:
        return True
    # not enough letters
    if sum(ch.isalpha() for ch in t) < 3:
        return True
    return False


def safe_remove(path: str) -> None:
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def cleanup_temp_files(keep_last: int = 20) -> None:
    files = st.session_state.get("temp_audio_files", [])
    if not files:
        return
    # keep only last N
    if len(files) <= keep_last:
        return
    to_delete = files[:-keep_last]
    st.session_state.temp_audio_files = files[-keep_last:]
    for p in to_delete:
        safe_remove(p)


def synthesize_tts(text: str) -> str:
    """Returns a temp mp3 path (caller should register it for cleanup)."""
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

    st.session_state.temp_audio_files.append(audio_path)
    cleanup_temp_files(keep_last=20)
    return audio_path


def translate_to_english(text: str) -> str:
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0,
        messages=[
            {"role": "system", "content": "Translate this Italian text into natural English."},
            {"role": "user", "content": text},
        ],
    )
    return resp.choices[0].message.content.strip()


def should_show_tutor(turn_count: int, english: bool, repaired: bool) -> bool:
    # V1.1: less noisy but still helpful
    if english or repaired:
        return True
    return turn_count % 3 == 0


# ================== STAGE MACHINES (V1.1 minimal) ==================

def update_stage_cafe(user_text: str, current: str) -> str:
    text = user_text.strip().lower()

    payment_cues = [
        "ecco", "tenga", "tieni", "prego",
        "eccoti", "eccolo", "eccola",
        "pago", "posso pagare",
        "in contanti", "contanti",
        "con la carta", "carta", "bancomat", "apple pay",
    ]

    if current == "PRICE_GIVEN" and any(cue in text for cue in payment_cues):
        return "PAYMENT"

    if any(x in text for x in ["quanto", "how much", "costa", "prezzo"]):
        return "PRICE_GIVEN"

    if any(x in text for x in ["pago", "carta", "contanti", "bancomat", "apple pay"]):
        return "PAYMENT"

    if any(x in text for x in ["grazie", "arrivederci", "ciao"]):
        return "CLOSING"

    return current


def update_stage_transport(user_text: str, current: str) -> str:
    text = user_text.strip().lower()

    if any(x in text for x in ["dove", "quale", "binario", "linea", "come", "per", "verso", "a "]):
        return "ASKING_ROUTE"

    if any(x in text for x in ["biglietto", "ticket", "abbonamento", "validare", "convalidare", "macchinetta"]):
        return "TICKET"

    if any(x in text for x in ["ok", "va bene", "perfetto", "capito", "grazie"]):
        return "CONFIRMATION" if current != "CLOSING" else current

    if any(x in text for x in ["arrivederci", "ciao"]):
        return "CLOSING"

    return current


def update_stage_directions(user_text: str, current: str) -> str:
    text = user_text.strip().lower()

    if any(x in text for x in ["dove", "come", "per andare", "per arrivare", "direzione"]):
        return "ASKING"

    if any(x in text for x in ["scusi", "puoi ripetere", "che strada", "destra", "sinistra"]):
        return "CLARIFYING"

    if any(x in text for x in ["ok", "va bene", "capito", "perfetto", "grazie"]):
        return "CONFIRMING" if current != "CLOSING" else current

    if any(x in text for x in ["arrivederci", "ciao"]):
        return "CLOSING"

    return current


def update_stage(scenario_key: str, user_text: str, current: str) -> str:
    if scenario_key == "cafe":
        return update_stage_cafe(user_text, current)
    if scenario_key == "transport":
        return update_stage_transport(user_text, current)
    if scenario_key == "directions":
        return update_stage_directions(user_text, current)
    return current


# ================== SESSION STATE ==================

if "scenario_key" not in st.session_state:
    st.session_state.scenario_key = "cafe"

if "stage" not in st.session_state:
    st.session_state.stage = SCENARIOS[st.session_state.scenario_key]["initial_stage"]

if "turn_count" not in st.session_state:
    st.session_state.turn_count = 0

if "partner_history" not in st.session_state:
    # Only user/assistant turns for the Partner model
    st.session_state.partner_history = []

if "conversation" not in st.session_state:
    # Rich UI turns
    st.session_state.conversation = []

if "temp_audio_files" not in st.session_state:
    st.session_state.temp_audio_files = []

if "last_user_input" not in st.session_state:
    st.session_state.last_user_input = ""


# ================== UI HEADER ==================
st.title("🗣️ Language Conversation Tutor")
st.caption("Speak to an AI partner (target language) and get optional tutor tips.")

scenario_label_to_key = {cfg["label"]: k for k, cfg in SCENARIOS.items()}
scenario_label = st.selectbox("Choose a scenario", list(scenario_label_to_key.keys()))
selected_key = scenario_label_to_key[scenario_label]

# Scenario change resets state (V1.1)
if selected_key != st.session_state.scenario_key:
    # cleanup temp audio
    for p in st.session_state.temp_audio_files:
        safe_remove(p)
    st.session_state.temp_audio_files = []

    st.session_state.scenario_key = selected_key
    st.session_state.stage = SCENARIOS[selected_key]["initial_stage"]
    st.session_state.turn_count = 0
    st.session_state.partner_history = []
    st.session_state.conversation = []
    st.session_state.last_user_input = ""

# Controls
col_a, col_b = st.columns([1, 1])
with col_a:
    show_tutor = st.toggle("Show tutor tips", value=True)
with col_b:
    show_translation = st.toggle("Enable translation button", value=True)

playback_my_sentence = st.toggle("Play back my sentence (TTS)", value=False)

# Reset
if st.button("Reset conversation"):
    for p in st.session_state.temp_audio_files:
        safe_remove(p)
    st.session_state.clear()
    st.rerun()

st.divider()

# ================== USER INPUT (Audio) ==================
audio_value = st.audio_input("Record a voice message")

transcribed_text = ""
user_input = ""
repaired_flag = False

if audio_value is not None:
    audio_bytes = audio_value.getvalue()
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

    user_input = transcribed_text

    # Repair fallback
    if looks_non_target_or_garbled(transcribed_text):
        try:
            vocab_hint = ", ".join(vocab_words[:120]) if vocab_words else ""
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
                            f"Scenario: {scenario_label}\n"
                            f"Noisy transcript: {transcribed_text}\n"
                            f"Allowed/simple vocab (optional): {vocab_hint}\n"
                            "Output ONLY the repaired Italian sentence."
                        ),
                    },
                ],
            )
            repaired = repair_resp.choices[0].message.content.strip()
            if repaired:
                user_input = repaired
                repaired_flag = True
        except Exception:
            user_input = transcribed_text

    if repaired_flag and user_input and user_input != transcribed_text:
        st.caption(f"🛠️ Interpreted as: {user_input}")

user_input = (user_input or "").strip()

# ================== TURN PROCESSING ==================
if user_input and user_input != st.session_state.last_user_input:
    st.session_state.last_user_input = user_input
    st.session_state.turn_count += 1

    scenario_key = st.session_state.scenario_key
    current_stage = st.session_state.stage
    new_stage = update_stage(scenario_key, user_input, current_stage)
    st.session_state.stage = new_stage

    english_flag = is_likely_english(user_input)
    tutor_active = show_tutor and should_show_tutor(st.session_state.turn_count, english_flag, repaired_flag)

    # ----- Partner call (clean context; no tutor artifacts) -----
    partner_system = SCENARIOS[scenario_key]["partner_prompt"]
    stage_note = f"Current stage: {new_stage}. Follow the stage rules." 

    partner_messages = [
        {"role": "system", "content": partner_system},
        {"role": "system", "content": stage_note},
        *st.session_state.partner_history,
        {"role": "user", "content": user_input},
    ]

    partner_resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=partner_messages,
        temperature=0.4,
    )

    partner_text = partner_resp.choices[0].message.content.strip()
    if not partner_text:
        partner_text = "Va bene."

    # Update partner history (NO duplicates)
    st.session_state.partner_history.append({"role": "user", "content": user_input})
    st.session_state.partner_history.append({"role": "assistant", "content": partner_text})

    # ----- Tutor call (separate context) -----
    tutor_text = ""
    if tutor_active:
        tutor_system = (
            "You are a language tutor. Give short, encouraging feedback in English, "
            "but all example sentences must be in Italian. "
            "Analyze the user's sentence and the partner reply to provide helpful tips. "
            "Be concise. Do not explain grammar in depth.\n\n"
            "Output format:\n"
            "- Clean version: <Italian>\n"
            "- More natural version: <Italian>\n"
            "- Tip: <short English tip>\n"
            "If the user's sentence is already natural, say exactly: Looks good 👍"
        )

        tutor_user = (
            f"Scenario: {scenario_label}\n"
            f"Stage: {new_stage}\n"
            f"User said: {user_input}\n"
            f"Partner replied: {partner_text}\n"
        )

        tutor_resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": tutor_system},
                {"role": "user", "content": tutor_user},
            ],
            temperature=0.2,
        )
        tutor_text = tutor_resp.choices[0].message.content.strip()

    # ----- TTS -----
    partner_audio_path = ""
    try:
        partner_audio_path = synthesize_tts(partner_text)
    except Exception:
        partner_audio_path = ""

    user_audio_path = ""
    if playback_my_sentence:
        try:
            user_audio_path = synthesize_tts(user_input)
        except Exception:
            user_audio_path = ""

    # ----- Store rich turn -----
    st.session_state.conversation.append(
        {
            "user": user_input,
            "partner": partner_text,
            "tutor": tutor_text,
            "partner_audio": partner_audio_path,
            "user_audio": user_audio_path,
            "translation": None,
        }
    )

# ================== DISPLAY (LAST N TURNS) ==================

N_TURNS = 3
turns = st.session_state.conversation[-N_TURNS:]

if turns:
    for idx, turn in enumerate(turns, start=max(0, len(st.session_state.conversation) - N_TURNS)):
        st.markdown(f"### Turn {idx + 1}")

        st.markdown(f"**You:** {turn['user']}")
        if turn.get("user_audio") and os.path.exists(turn["user_audio"]):
            st.audio(turn["user_audio"])

        st.markdown(f"**Partner:** {turn['partner']}")
        if turn.get("partner_audio") and os.path.exists(turn["partner_audio"]):
            st.audio(turn["partner_audio"])

        if show_translation:
            if st.button("Translate partner", key=f"translate_{idx}"):
                if turn["translation"] is None:
                    try:
                        turn["translation"] = translate_to_english(turn["partner"])
                    except Exception:
                        turn["translation"] = "(Translation unavailable.)"

            if turn.get("translation"):
                st.markdown(f"🟦 *English:* {turn['translation']}")

        if turn.get("tutor"):
            st.markdown("**Tutor:**")
            st.markdown(turn["tutor"])

        st.divider()
else:
    st.info("Record a message to start.")

# Footer info
st.caption(f"Scenario: {SCENARIOS[st.session_state.scenario_key]['label']} · Stage: {st.session_state.stage}")
