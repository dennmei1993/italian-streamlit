import streamlit as st
import streamlit.components.v1 as components
from openai import OpenAI
from dotenv import load_dotenv
import json
import tempfile
import os
import re
import io
import hashlib
import base64

# ================== SETUP ==================
load_dotenv()
OPENAI_API_KEY = st.secrets.get("OPENAI_API_KEY", os.getenv("OPENAI_API_KEY"))
client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else OpenAI()

# ================== SCENARIO ASSETS ==================
# File structure (as per your repo):
#   assets/backgrounds/<name>.jpg
#   assets/avatars/<name>.png
STAGE_BACKGROUNDS = {
    "☕ Ordering coffee / food": [
        "assets/backgrounds/cafe.jpg",
    ],
    "🚆 Buying tickets / transport": [
        "assets/backgrounds/transport.jpg",
    ],
    "🚶 Asking directions": [
        "assets/backgrounds/directions.jpg",
    ],
}

STAGE_AVATARS = {
    "☕ Ordering coffee / food": [
        "assets/avatars/barista.png",
    ],
    "🚆 Buying tickets / transport": [
        "assets/avatars/ticket_clerk.png",
    ],
    "🚶 Asking directions": [
        "assets/avatars/local_person.png",
    ],
}


def _file_to_data_uri(path: str) -> str:
    """Convert a local image file to a data URI for HTML rendering."""
    if not path:
        return ""
    try:
        if not os.path.exists(path):
            return ""
        ext = os.path.splitext(path)[1].lower().lstrip(".")
        mime = {
            "png": "image/png",
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "webp": "image/webp",
        }.get(ext, "")
        if not mime:
            return ""
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("utf-8")
        return f"data:{mime};base64,{b64}"
    except Exception:
        return ""


def get_scenario_assets(scenario_label: str) -> tuple[str, str]:
    """Return (background_path, avatar_path) for the selected scenario label."""
    bg = (STAGE_BACKGROUNDS.get(scenario_label) or [""])[0]
    av = (STAGE_AVATARS.get(scenario_label) or [""])[0]
    return bg, av

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

# Lock the overall page (no outer 
# ================== CONVERSATION PAGE ==================
scenario = st.session_state.scenario
show_tutor = bool(st.session_state.show_tutor)
show_translation = bool(st.session_state.show_translation)
playback_my_sentence = bool(st.session_state.playback_my_sentence)

# --- Mobile-friendly viewport height detection (one-time) ---
# Streamlit can't directly read screen height in Python; we grab it in the browser once and pass it via query params.
try:
    qp = st.query_params
    vh_param = qp.get("vh", "")
except Exception:
    qp = None
    vh_param = ""

# Inject JS to set ?vh=<window.innerHeight> once per browser session
components.html(
    """
    <script>
      (function () {
        try {
          const key = "parley_vh_set";
          const already = sessionStorage.getItem(key);
          const h = window.innerHeight || 800;
          const url = new URL(window.location.href);
          const current = parseInt(url.searchParams.get("vh") || "0", 10);

          if (!already || Math.abs(current - h) > 40) {
            url.searchParams.set("vh", String(h));
            sessionStorage.setItem(key, "1");
            window.location.replace(url.toString());
          }
        } catch (e) {}
      })();
    </script>
    """,
    height=0,
)

# Compute panel heights (fallback if vh not available yet)
def _safe_int(x, default=0):
    try:
        return int(x)
    except Exception:
        return default

vh = _safe_int(vh_param, 0)
# Reserve some pixels for Streamlit padding / mobile browser UI.
# This is intentionally conservative so panels fit without outer scrolling.
reserved = 140
total_h = max(520, min(1000, (vh - reserved) if vh else 720))
scenario_h = int(total_h * 0.60)
interaction_h = total_h - scenario_h

# Reduce outer padding and avoid accidental page scroll
st.markdown(
    """
    <style>
      html, body { overflow: hidden; }
      .block-container { padding-top: 0.5rem; padding-bottom: 0.5rem; }
      /* Make container scrollbars nicer on iOS */
      div[data-testid="stVerticalBlock"] { gap: 0.6rem; }
      .scenario-wrap {
        position: relative;
        width: 100%;
        height: 100%;
        border-radius: 14px;
        overflow: hidden;
        background: #e6e6e6;
      }
      .scenario-bg {
        position: absolute;
        inset: 0;
        background-size: cover;
        background-position: center;
        transform: scale(1.02);
      }
      .scenario-avatar {
        position: absolute;
        right: 10px;
        bottom: 58px;
        width: 28%;
        max-width: 180px;
        height: auto;
        filter: drop-shadow(0 6px 10px rgba(0,0,0,0.35));
        pointer-events: none;
      }
      .scenario-badge {
        position: absolute;
        left: 10px;
        top: 10px;
        padding: 0.25rem 0.55rem;
        background: rgba(0,0,0,0.45);
        color: #fff;
        border-radius: 999px;
        font-size: 0.85rem;
      }
      .scenario-controls {
        position: absolute;
        left: 0;
        right: 0;
        bottom: 0;
        padding: 0.55rem;
        background: linear-gradient(to top, rgba(0,0,0,0.45), rgba(0,0,0,0));
        display: flex;
        gap: 0.7rem;
        justify-content: center;
        align-items: center;
      }
      .scenario-controls .icon-btn {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        width: 44px;
        height: 44px;
        border-radius: 12px;
        text-decoration: none;
        font-size: 1.35rem;
        line-height: 1;
        background: rgba(255,255,255,0.18);
        border: 1px solid rgba(255,255,255,0.25);
        backdrop-filter: blur(6px);
        -webkit-backdrop-filter: blur(6px);
      }
      .scenario-missing {
        position: absolute;
        inset: 0;
        display: flex;
        align-items: center;
        justify-content: center;
        text-align: center;
        padding: 1rem;
        color: rgba(0,0,0,0.75);
        font-weight: 600;
        background: linear-gradient(135deg, rgba(240,240,240,0.95), rgba(220,220,220,0.95));
      }
    </style>
    """,
    unsafe_allow_html=True,
)

# Handle icon actions via query params (works reliably on mobile)
try:
    action = (st.query_params.get("action", "") or "").strip()
except Exception:
    action = ""

if action in {"home", "new", "end"}:
    try:
        st.query_params.pop("action", None)
    except Exception:
        try:
            st.query_params.clear()
        except Exception:
            pass

    if action == "home":
        st.session_state.page = "home"
        st.rerun()
    if action == "new":
        reset_conversation_state(clear_log=True)
        st.rerun()
    if action == "end":
        archive_active_interaction()
        st.session_state.page = "review"
        st.rerun()

# --- Scenario Panel (fixed height, shows background + avatar + icon bar) ---
scenario_panel = st.container(height=scenario_h)

with scenario_panel:
    bg_path, av_path = get_scenario_assets(st.session_state.scenario)
    bg_uri = _file_to_data_uri(bg_path)
    av_uri = _file_to_data_uri(av_path)

    missing_html = ""
    if not bg_uri:
        missing_html = f"<div class='scenario-missing'>Missing background image:<br><code>{bg_path}</code></div>"

    html = f"""
    <div class="scenario-wrap">
      <div class="scenario-bg" style="background-image:url('{bg_uri or ''}');"></div>
      {missing_html}
      {f"<img class='scenario-avatar' src='{av_uri}'/>" if av_uri else ""}
      <div class="scenario-badge">{st.session_state.scenario}</div>
      <div class="scenario-controls">
        <a class="icon-btn" href="?action=home" aria-label="Home">🏠</a>
        <a class="icon-btn" href="?action=new" aria-label="New Conversation">🆕</a>
        <a class="icon-btn" href="?action=end" aria-label="End Conversation">⏹</a>
      </div>
    </div>
    """
    st.markdown(html, unsafe_allow_html=True)

# --- Interaction Panel (fixed height; container provides internal scroll) ---
interaction_panel = st.container(height=interaction_h)

with interaction_panel:
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
