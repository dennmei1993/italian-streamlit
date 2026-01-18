import streamlit as st
from openai import OpenAI
from dotenv import load_dotenv
import json
import tempfile
import os
import re
import io
import hashlib
import base64
import mimetypes
import streamlit.components.v1 as components

# ================== PAGE CONFIG ==================
st.set_page_config(
    page_title="Parley",
    page_icon="🗣️",
    layout="centered",
    initial_sidebar_state="collapsed",
)

# ================== SETUP ==================
load_dotenv()
OPENAI_API_KEY = st.secrets.get("OPENAI_API_KEY", os.getenv("OPENAI_API_KEY"))
client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else OpenAI()

# ================== HELPERS ==================

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

    if len(t) <= 1:
        return True

    english_markers = {
        "i", "you", "we", "they", "want", "need", "with", "and", "the", "a",
        "to", "for", "is", "are", "please",
    }
    tokens = re.findall(r"[a-z']+", t)
    if tokens and sum(tok in english_markers for tok in tokens) >= 2:
        return True

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

    rec = re.search(r"(?:^|\n)\s*[-•]?\s*Recommended\s*:\s*(.+)", raw, flags=re.IGNORECASE)
    tip = re.search(r"(?:^|\n)\s*[-•]?\s*Tip\s*:\s*(.+)", raw, flags=re.IGNORECASE)

    if rec:
        out["recommended"] = rec.group(1).strip()
    if tip:
        out["tip"] = tip.group(1).strip()
    return out


def _abs_asset_path(rel_path: str) -> str:
    """Resolve repo-relative asset paths reliably on Streamlit Cloud."""
    if not rel_path:
        return ""
    if os.path.isabs(rel_path):
        return rel_path
    base_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_dir, rel_path)


def _file_to_data_uri(rel_path: str) -> str:
    """Return a data: URI for an image file, or empty string if missing."""
    abs_path = _abs_asset_path(rel_path)
    if not abs_path or (not os.path.exists(abs_path)):
        return ""
    mime, _ = mimetypes.guess_type(abs_path)
    if not mime:
        mime = "image/png"
    try:
        with open(abs_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        return f"data:{mime};base64,{b64}"
    except Exception:
        return ""


def _get_query_param(name: str) -> str:
    """Compat helper for reading query params across Streamlit versions."""
    try:
        v = st.query_params.get(name)
        if isinstance(v, list):
            return v[0] if v else ""
        return v or ""
    except Exception:
        try:
            qp = st.experimental_get_query_params()
            v = qp.get(name, [""])
            return v[0] if v else ""
        except Exception:
            return ""


def _clear_query_params() -> None:
    """Compat helper to clear query params after handling an action."""
    try:
        st.query_params.clear()
    except Exception:
        try:
            st.experimental_set_query_params()
        except Exception:
            pass


# ================== OPTIONAL VOCAB ==================
vocab = []
try:
    with open("vocab.json", encoding="utf-8") as f:
        vocab = json.load(f).get("words", [])
except Exception:
    vocab = []


# ================== SESSION STATE ==================
if "page" not in st.session_state:
    st.session_state.page = "home"  # home | conversation | review

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

# Archive of completed interactions (for End Conversation review)
if "conversation_log" not in st.session_state:
    st.session_state.conversation_log = []

# The current interaction displayed in the Interaction panel (cleared on next submit)
if "active_interaction" not in st.session_state:
    st.session_state.active_interaction = None

if "turn_count" not in st.session_state:
    st.session_state.turn_count = 0

if "last_user_input" not in st.session_state:
    st.session_state.last_user_input = ""

if "last_audio_hash" not in st.session_state:
    st.session_state.last_audio_hash = None

if "stage" not in st.session_state:
    st.session_state.stage = "ORDERING"

# ================== GLOBAL MOBILE TWEAKS ==================
st.markdown(
    """
<style>
@media (max-width: 480px) {
  /* Reduce top whitespace on mobile so Home fits on one screen */
  section.main > div.block-container { padding-top: 0.35rem !important; padding-bottom: 0.6rem !important; }
  h1 { font-size: 2.05rem !important; line-height: 1.02 !important; margin-bottom: 0.35rem !important; }
  h2 { margin-top: 0.6rem !important; }
  p { margin-top: 0.2rem !important; margin-bottom: 0.4rem !important; }
}
</style>
    """,
    unsafe_allow_html=True,
)


def reset_conversation_state(clear_log: bool) -> None:
    """Reset the in-session conversation state. Optionally clear the archived log."""
    st.session_state.messages = []
    st.session_state.turn_count = 0
    st.session_state.last_user_input = ""
    st.session_state.last_audio_hash = None
    st.session_state.stage = "ORDERING"
    st.session_state.active_interaction = None
    if clear_log:
        st.session_state.conversation_log = []


def archive_active_interaction() -> None:
    """Move the current interaction into the archived log (if present)."""
    if st.session_state.active_interaction:
        st.session_state.conversation_log.append(st.session_state.active_interaction)
        st.session_state.active_interaction = None


# ================== HOME PAGE ==================
if st.session_state.page == "home":
    st.markdown(
        """
        <style>
          /* Home page: reduce vertical spacing on mobile so everything fits */
          @media (max-width: 480px) {
            section.main > div.block-container {
              padding-top: 0.25rem !important;
              padding-bottom: 0.5rem !important;
            }
            h1 {
              font-size: 2.0rem !important;
              line-height: 1.02 !important;
              margin-top: 0.15rem !important;
              margin-bottom: 0.35rem !important;
            }
            div[data-testid="stCaptionContainer"] {
              margin-bottom: 0.35rem !important;
            }
            div[data-testid="stDivider"] {
              margin-top: 0.6rem !important;
              margin-bottom: 0.6rem !important;
            }
          }
          html, body { overflow-x: hidden; }
        </style>
        """,
        unsafe_allow_html=True,
    )

    st.title("🗣️ Language Conversation Tutor")
    st.caption("Select a scenario and settings, then start the conversation.")

    st.session_state.scenario = st.selectbox(
        "Choose a scenario",
        [
            "Ordering coffee / food",
            "Buying tickets / transport",
            "Asking directions",
        ],
        index=[
            "Ordering coffee / food",
            "Buying tickets / transport",
            "Asking directions",
        ].index(st.session_state.scenario)
        if st.session_state.scenario in [
            "Ordering coffee / food",
            "Buying tickets / transport",
            "Asking directions",
        ]
        else 0,
    )

    # st.divider()
    st.session_state.show_tutor = st.toggle("Show tutor tips", value=st.session_state.show_tutor)
    st.session_state.show_translation = st.toggle("Enable translation", value=st.session_state.show_translation)
    st.session_state.playback_my_sentence = st.toggle(
        "Play back my sentence (TTS)", value=st.session_state.playback_my_sentence
    )

    # st.divider()
    if st.button("▶ Start"):
        # Start a fresh conversation (also clears any prior log to avoid confusion).
        reset_conversation_state(clear_log=True)
        st.session_state.page = "conversation"
        st.rerun()

    st.stop()


# ================== REVIEW PAGE (End Conversation) ==================
if st.session_state.page == "review":
    st.title("📜 Conversation Review")
    st.caption("Here is your full conversation history from the last session.")

    col_a, col_b = st.columns([1, 2])
    with col_a:
        if st.button("⬅ Home", key="review_home"):
            st.session_state.page = "home"
            st.rerun()
    with col_b:
        if st.button("🆕 New Conversation", key="review_new"):
            reset_conversation_state(clear_log=True)
            st.session_state.page = "conversation"
            st.rerun()

    st.divider()

    log = st.session_state.conversation_log[:]  # copy
    if not log:
        st.info("No conversation history yet. Start a conversation and press End Conversation.")
        st.stop()

    for i, turn in enumerate(log, start=1):
        st.markdown(f"### Turn {i}")
        st.markdown(f"**You:** {turn.get('user','')}")
        if turn.get("user_audio") and os.path.exists(turn["user_audio"]):
            st.audio(turn["user_audio"])

        st.markdown(f"**Partner:** {turn.get('partner','')}")
        if turn.get("partner_audio") and os.path.exists(turn["partner_audio"]):
            st.audio(turn["partner_audio"])

        tutor_raw = (turn.get("tutor_raw") or "").strip()
        if tutor_raw:
            st.markdown("**Tutor:**")
            if tutor_raw.lower().startswith("looks good"):
                st.markdown(tutor_raw)
            else:
                rec = (turn.get("tutor_recommended") or "").strip()
                tip = (turn.get("tutor_tip") or "").strip()
                if rec:
                    st.markdown(f"**Recommended:** {rec}")
                    if turn.get("tutor_recommended_audio") and os.path.exists(turn["tutor_recommended_audio"]):
                        st.audio(turn["tutor_recommended_audio"])
                if tip:
                    st.markdown(f"**Tip:** {tip}")

        st.divider()

    st.stop()


# ================== CONVERSATION PAGE ==================
scenario = st.session_state.scenario
show_tutor = bool(st.session_state.show_tutor)
show_translation = bool(st.session_state.show_translation)
playback_my_sentence = bool(st.session_state.playback_my_sentence)

# ------------------ Scenario Assets ------------------
# Repo structure (as per your GitHub screenshots):
#   assets/backgrounds/{cafe,directions,transport}.jpg
#   assets/avatars/{barista,local_person,ticket_clerk}.png
STAGE_BACKGROUNDS = {
    "Ordering coffee / food": ["assets/backgrounds/cafe.jpg"],
    "Buying tickets / transport": ["assets/backgrounds/transport.jpg"],
    "Asking directions": ["assets/backgrounds/directions.jpg"],
}
STAGE_AVATARS = {
    "Ordering coffee / food": ["assets/avatars/barista.png"],
    "Buying tickets / transport": ["assets/avatars/ticket_clerk.png"],
    "Asking directions": ["assets/avatars/local_person.png"],
}


def _normalize_scenario_label(label: str) -> str:
    """Normalize scenario labels so emoji/no-emoji variants still match asset dict keys."""
    if not label:
        return ""
    # Remove leading emoji + spaces, e.g. "☕ Ordering coffee / food" -> "Ordering coffee / food"
    return re.sub(r"^[^A-Za-z]*", "", label).strip()


def _get_stage_asset(stage_map: dict, scenario_label: str) -> str:
    key = _normalize_scenario_label(scenario_label)
    candidates = stage_map.get(key) or []
    return candidates[0] if candidates else ""


# ------------------ Scenario Panel ------------------

# Mobile/Streamlit note:
# On iOS/mobile, Streamlit can collapse `st.columns()` into a vertical stack.
# To guarantee a single-row nav on phones, we render the nav as HTML links (query params)
# and handle the click server-side via `st.query_params`.

st.markdown(
    """
    <style>
      /* Mobile: tighten overall top padding so Home fits better on one screen */
      @media (max-width: 480px) {
        section.main > div.block-container { padding-top: 0.25rem !important; padding-bottom: 0.5rem !important; }
        h1 { font-size: 1.9rem !important; line-height: 1.02 !important; margin-top: 0.15rem !important; margin-bottom: 0.35rem !important; }
        div[data-testid="stVerticalBlock"] > div:has(> div[data-testid="stSelectbox"]) { margin-top: 0.15rem !important; }
      }

      /* Prevent accidental horizontal overflow on mobile */
      html, body { overflow-x: hidden; }

      /* ----- NAV ROW (HTML) ----- */
      .navrow {
        display: flex;
        flex-direction: row;
        flex-wrap: nowrap;
        justify-content: center;
        align-items: center;
        gap: 10px;
        margin: 6px 0 10px 0;
        width: 100%;
      }
      .navbtn {
        display: inline-flex;
        justify-content: center;
        align-items: center;
        width: 54px;
        min-width: 54px;
        height: 44px;
        border-radius: 14px;
        text-decoration: none;
        font-size: 22px;
        background: rgba(255,255,255,0.05);
        border: 1px solid rgba(255,255,255,0.14);
        box-shadow: 0 2px 12px rgba(0,0,0,0.25);
      }
      .navbtn:active {
        transform: scale(0.98);
      }
    </style>
    """,
    unsafe_allow_html=True,
)

# --- Nav click handling via query params ---
nav_action = _get_query_param("nav")
if nav_action:
    # Clear params first to avoid repeated triggers on rerun
    _clear_query_params()
    if nav_action == "home":
        st.session_state.page = "home"
    elif nav_action == "new":
        reset_conversation_state(clear_log=True)
        st.session_state.page = "conversation"
    elif nav_action == "end":
        archive_active_interaction()
        st.session_state.page = "review"
    st.rerun()

# Render compact nav row (always one row, not Streamlit columns/buttons)
st.markdown(
    """
    <div class="navrow">
      <a class="navbtn" href="?nav=home" aria-label="Home">🏠</a>
      <a class="navbtn" href="?nav=new" aria-label="New Conversation">🆕</a>
      <a class="navbtn" href="?nav=end" aria-label="End Conversation">⏹</a>
    </div>
    """,
    unsafe_allow_html=True,
)

bg_rel = _get_stage_asset(STAGE_BACKGROUNDS, scenario)
av_rel = _get_stage_asset(STAGE_AVATARS, scenario)
bg_abs = _abs_asset_path(bg_rel)
av_abs = _abs_asset_path(av_rel)

# st.caption(f"Scenario: {_normalize_scenario_label(scenario)}")

# Render the scene (background + avatar overlay) in one HTML block so the avatar sits ON TOP.
bg_uri = _file_to_data_uri(bg_rel)
av_uri = _file_to_data_uri(av_rel)

if not bg_uri:
    st.warning(f"Scenario background not found: {bg_rel}")
if av_rel and (not av_uri):
    st.warning(f"Scenario avatar not found: {av_rel}")

scene_html = f"""
<style>
  .scene-wrap {{
    position: relative;
    width: 100%;
    height: 340px;
    border-radius: 16px;
    overflow: hidden;
    background: rgba(255,255,255,0.04);
    border: 1px solid rgba(255,255,255,0.10);
  }}
  .scene-bg {{
    position: absolute;
    inset: 0;
    background-image: url('{bg_uri}');
    background-size: cover;
    background-position: center;
    filter: none;
  }}
  .scene-dim {{
    position: absolute;
    inset: 0;
    background: linear-gradient(to bottom, rgba(0,0,0,0.10), rgba(0,0,0,0.45));
  }}
  .scene-avatar {{
    position: absolute;
    right: 12px;
    bottom: 10px;
    width: 160px;
    max-width: 45%;
    height: auto;
    filter: drop-shadow(0 10px 18px rgba(0,0,0,0.45));
  }}
  .scene-missing {{
    position: absolute;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    color: rgba(255,255,255,0.75);
    font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif;
    font-size: 14px;
    padding: 16px;
    text-align: center;
  }}
</style>

<div class="scene-wrap">
  {'<div class="scene-bg"></div><div class="scene-dim"></div>' if bg_uri else '<div class="scene-missing">(Missing scenario background)</div>'}
  {'<img class="scene-avatar" src="' + av_uri + '" alt="avatar" />' if av_uri else ''}
</div>
"""

# Height is fixed for iframe sizing; adjust if you want taller/shorter scene.
components.html(scene_html, height=360, scrolling=False)


# ------------------ Interaction Section ------------------
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
OUTPUT FORMAT (MANDATORY)
==============================

PARTNER:
<Italian reply>

OPTIONAL_TUTOR:
<feedback or empty>
"""

if not st.session_state.messages:
    st.session_state.messages.append({"role": "system", "content": system_prompt})


# ================== USER INPUT ==================
audio_value = st.audio_input("Press Mic sign to record a voice message, and press again to stop.")

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
        # New submission: archive previous interaction and clear the interaction panel content
        archive_active_interaction()
        st.session_state.last_audio_hash = audio_hash
        st.session_state.last_user_input = ""  # allow same sentence in new turn if needed

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
                                f"Scenario: {scenario}\n"
                                f"Noisy transcript: {transcribed_text}\n"
                                f"Allowed/simple vocab (optional): {vocab_hint}\n"
                                "Output ONLY the repaired Italian sentence."
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

        if tutor_struct.get("recommended"):
            try:
                recommended_audio = speak_italian(tutor_struct["recommended"])
            except Exception:
                recommended_audio = ""

    # ----- Audio -----
    partner_audio = ""
    user_audio = ""
    try:
        partner_audio = speak_italian(partner_text)
    except Exception:
        partner_audio = ""

    if playback_my_sentence:
        try:
            user_audio = speak_italian(user_input)
        except Exception:
            user_audio = ""

    # Store as the active interaction (Interaction panel shows only this)
    st.session_state.active_interaction = {
        "user": user_input,
        "partner": partner_text,
        "tutor_raw": tutor_text,
        "tutor_recommended": tutor_struct.get("recommended", ""),
        "tutor_tip": tutor_struct.get("tip", ""),
        "tutor_recommended_audio": recommended_audio,
        "partner_audio": partner_audio,
        "user_audio": user_audio,
        "translation": None,
    }

# ================== DISPLAY (ACTIVE INTERACTION ONLY) ==================
turn = st.session_state.active_interaction
if turn:
    st.markdown(f"**You:** {turn['user']}")
    if turn.get("user_audio") and os.path.exists(turn["user_audio"]):
        st.audio(turn["user_audio"])

    st.markdown(f"**Partner:** {turn['partner']}")
    if turn.get("partner_audio") and os.path.exists(turn["partner_audio"]):
        st.audio(turn["partner_audio"])

    if show_translation:
        if st.button("Translate", key="translate_active"):
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
else:
    st.info("Record a message to start.")


