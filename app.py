import streamlit as st
from openai import OpenAI
from dotenv import load_dotenv
import json
import tempfile
import os
import re
import io
import base64
import streamlit.components.v1 as components

OPENAI_API_KEY = st.secrets.get("OPENAI_API_KEY", os.getenv("OPENAI_API_KEY"))

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


# ================== SETUP ==================
load_dotenv()
client = OpenAI()

# ================== ASSET HELPERS (UI only) ==================
def resolve_asset(path: str) -> str | None:
    """Return an existing path, trying common image extensions if needed."""
    if not path:
        return None
    if os.path.exists(path):
        return path
    # Try swapping .png <-> .jpg/.jpeg (useful if you renamed files)
    base, ext = os.path.splitext(path)
    candidates = []
    if ext.lower() == '.png':
        candidates = [base + '.jpg', base + '.jpeg']
    elif ext.lower() in ('.jpg', '.jpeg'):
        candidates = [base + '.png']
    for c in candidates:
        if os.path.exists(c):
            return c
    return None

def img_to_base64(path: str) -> str:
    with open(path, 'rb') as f:
        return base64.b64encode(f.read()).decode('utf-8')

import mimetypes

def img_file_to_data_uri(path: str) -> str:
    """
    Convert an image file to a data URI usable in HTML <img src="...">
    """
    mime_type, _ = mimetypes.guess_type(path)
    if not mime_type:
        mime_type = "image/png"  # safe default

    b64 = img_to_base64(path)
    return f"data:{mime_type};base64,{b64}"


# ================== USER PRONUNCIATION (UI only) ==================
def speak_italian(text: str) -> str | None:
    """Generate Italian TTS for the given sentence and return a temp mp3 path."""
    if not text or not text.strip():
        return None
    audio_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix='.mp3') as tmp:
            audio_path = tmp.name
        speech = client.audio.speech.create(
            model='gpt-4o-mini-tts',
            voice='alloy',
            input=text.strip()
        )
        with open(audio_path, 'wb') as f:
            f.write(speech.read())
        return audio_path
    except Exception:
        # keep app running even if TTS fails
        if audio_path and os.path.exists(audio_path):
            try:
                os.remove(audio_path)
            except Exception:
                pass
        return None

with open("vocab.json", encoding="utf-8") as f:
    vocab = json.load(f)["words"]

#st.title("Italian Conversation Practice 🇮🇹")
#st.write("Partner speaks Italian. Tutor helps when needed.")

scenario = st.selectbox(
    "Choose a scenario",
    [
        "☕ Ordering coffee / food",
        "🚆 Buying tickets / transport",
        "🚶 Asking directions"
    ]
)

# ================== SCENARIO VISUALS (UI only) ==================
AVATARS = {
    '☕ Ordering coffee / food': 'assets/avatars/barista.png',
    '🚆 Buying tickets / transport': 'assets/avatars/ticket_clerk.png',
    '🚶 Asking directions': 'assets/avatars/local_person.png',
}

BACKGROUNDS = {
    '☕ Ordering coffee / food': 'assets/backgrounds/cafe.jpg',
    '🚆 Buying tickets / transport': 'assets/backgrounds/transport.jpg',
    '🚶 Asking directions': 'assets/backgrounds/directions.jpg',
}

avatar_path = resolve_asset(AVATARS.get(scenario, ''))
background_path = resolve_asset(BACKGROUNDS.get(scenario, ''))

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

# ================== STAGE + PANEL LAYOUT (Option 1: JS wrap all widgets into panel) ==================
# We render a fixed "Stage" (top 60%) with background + avatar, then a fixed "Panel" (bottom 40%)
# and use small JS to move all subsequent Streamlit blocks into the panel.

background_uri = None
avatar_uri = None
if background_path:
    try:
        background_uri = img_file_to_data_uri(background_path)
    except Exception:
        background_uri = None

if avatar_path:
    try:
        avatar_uri = img_file_to_data_uri(avatar_path)
    except Exception:
        avatar_uri = None

stage_bg_style = f"background-image: linear-gradient(rgba(0,0,0,0.18), rgba(0,0,0,0.55)), url('{background_uri}');" if background_uri else "background: #0b0f17;"
avatar_html = f"<img class='avatar-float' src='{avatar_uri}' alt='avatar' />" if avatar_uri else ""


# ================== DISPLAY (Stage 60% + Interaction Panel 40%) ==================

# NOTE: We build a fixed Stage (top) and turn Streamlit's main block-container
# into the fixed Interaction Panel (bottom). This avoids any JS relocation and
# is the most reliable approach on iPhone/Safari.

stage_html = """
<style>
:root {
  /* iOS Safari can misreport vh; we set --vh via JS to window.innerHeight*0.01px */
  --vh: 1vh;
  --stage-h: calc(var(--vh) * 60);
}

html, body { height: 100%; overflow: hidden; }
header[data-testid='stHeader'] { display: none; }
footer { display: none; }

/* The Streamlit main content becomes the Interaction Panel */
section.main > div.block-container {
  position: fixed;
  top: var(--stage-h);
  left: 0;
  right: 0;
  height: calc((var(--vh) * 100) - var(--stage-h));
  overflow-y: auto;
  overflow-x: hidden;
  padding: 14px 14px 18px 14px !important;
  background: rgba(13, 17, 23, 0.92);
  border-top: 1px solid rgba(255,255,255,0.10);
  max-width: 100% !important;
  z-index: 20;
}

/* Remove default top padding/margins that can create blank areas */
section.main { padding-top: 0 !important; }

/* Stage: fixed top area */
#stage-root {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  height: var(--stage-h);
  background-image:
    linear-gradient(rgba(0,0,0,0.10), rgba(0,0,0,0.55)),
    {{STAGE_BG_STYLE}};
  background-size: cover;
  background-position: center;
  background-repeat: no-repeat;
  overflow: hidden;
  z-index: 10;
}

/* Scenario selector pinned INSIDE stage */
div[data-testid='stSelectbox'] {
  position: fixed;
  top: 12px;
  left: 12px;
  right: 12px;
  z-index: 50;
}

div[data-testid='stSelectbox'] label { display: none; }

/* Avatar inside stage */
.avatar-float {
  position: absolute;
  left: 50%;
  top: 62%;
  transform: translate(-50%, -50%);
  width: min(72vw, 520px);
  height: auto;
  border: none;
  background: transparent;
  border-radius: 24px;
  box-shadow: 0 12px 30px rgba(0,0,0,0.26);
  z-index: 20;
  pointer-events: none;
}

/* Make widgets in the panel readable on dark background */
section.main > div.block-container, section.main > div.block-container * {
  color: #f1f1f1;
}

</style>

<script>
(function(){
  function setVH(){
    var vh = window.innerHeight * 0.01;
    document.documentElement.style.setProperty('--vh', vh + 'px');
  }
  setVH();
  window.addEventListener('resize', setVH);
  window.addEventListener('orientationchange', setVH);
})();
</script>

<script>
(function(){
  function setVH(){
    var vh = (window.innerHeight || document.documentElement.clientHeight) * 0.01;
    document.documentElement.style.setProperty('--vh', vh + 'px');
  }
  setVH();
  window.addEventListener('resize', setVH);
})();
</script>

<div id="stage-root">
  {{AVATAR_HTML}}
</div>
"""


# Inject avatar HTML into stage_html
avatar_html = f"<img class='avatar-float' src='{avatar_uri}' />" if avatar_uri else ""
stage_html = stage_html.replace("{{AVATAR_HTML}}", avatar_html)

# Inject background style
stage_html = stage_html.replace("{{STAGE_BG_STYLE}}", stage_bg_style)

st.markdown(stage_html, unsafe_allow_html=True)


# ================== USER INPUT ==================

import io

st.subheader("🎙️ Speak (optional)")
audio_value = st.audio_input("Record a voice message")

transcribed_text = ""
final_audio_input = ""  # what we will actually send into your app flow (Mode A)

if audio_value is not None:
    audio_bytes = audio_value.getvalue()
    audio_file = io.BytesIO(audio_bytes)
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
    if transcribed_text:
        st.caption(f"🎧 Heard: {transcribed_text}")
    if final_audio_input and final_audio_input != transcribed_text:
        st.caption(f"🛠️ Interpreted as: {final_audio_input}")

typed_input = st.text_input("You:")
user_input = final_audio_input.strip() if final_audio_input.strip() else typed_input.strip()


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

# ---------- Panel content (rendered into our fixed wrapper) ----------

if st.session_state.conversation:
    turn = st.session_state.conversation[-1]

    st.markdown(f"**You:** {turn['user']}")

    if st.button('🔊 Listen (Italian pronunciation)', key='speak_user_latest'):
        user_audio = speak_italian(turn['user'])
        if user_audio and os.path.exists(user_audio):
            st.audio(user_audio)

    st.markdown(f"**AI (Partner):** {turn['partner']}")

    if turn.get('audio') and os.path.exists(turn['audio']):
        st.audio(turn['audio'])

    if st.button('Show English', key='translate_latest'):
        if turn.get('translation') is None:
            turn['translation'] = translate_to_english(turn['partner'])

    if turn.get('translation'):
        st.markdown(f"🟦 *English:* {turn['translation']}")

    if turn.get('tutor'):
        st.markdown('**Tutor:**')
        st.markdown(turn['tutor'])
else:
    st.write('Say something to start.')

# ================== RESET ==================
if st.button("Reset Conversation"):
    for turn in st.session_state.conversation:
        if turn.get("audio") and os.path.exists(turn["audio"]):
            os.remove(turn["audio"])
    st.session_state.clear()
    st.stop()

# Close fixed panel wrapper
st.markdown("</div>", unsafe_allow_html=True)
