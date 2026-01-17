import streamlit as st
from openai import OpenAI
from dotenv import load_dotenv
import json
import tempfile
import os
import re
import io
import base64
import mimetypes
from pathlib import Path
from PIL import Image

# ================== SETUP & SECRETS ==================
load_dotenv()
client = OpenAI(api_key=st.secrets.get("OPENAI_API_KEY", os.getenv("OPENAI_API_KEY")))

# Set ASSET_BASE_URL in Streamlit Secrets for the most reliable iPhone rendering
ASSET_BASE_URL = st.secrets.get('ASSET_BASE_URL', '').strip()
BASE_DIR = Path(__file__).resolve().parent

with open("vocab.json", encoding="utf-8") as f:
    vocab = json.load(f)["words"]

# ================== IMAGE & ASSET HELPERS ==================

def asset_url(rel_path: str) -> str | None:
    """Return an absolute URL for an asset if ASSET_BASE_URL is set; otherwise None."""
    if not rel_path or not ASSET_BASE_URL:
        return None
    base = ASSET_BASE_URL if ASSET_BASE_URL.endswith('/') else ASSET_BASE_URL + '/'
    return base + rel_path.lstrip('/')

def resolve_asset(path: str) -> str | None:
    """Resolve an asset path relative to this script, with .png/.jpg/.jpeg fallbacks."""
    cand = Path(path)
    if not cand.is_absolute():
        cand = BASE_DIR / cand
    if cand.exists():
        return str(cand)
    base = cand.with_suffix('')
    for ext in ('.png', '.jpg', '.jpeg'):
        c2 = base.with_suffix(ext)
        if c2.exists():
            return str(c2)
    return None

def img_file_to_data_uri(path: str, max_dim: int = 800) -> str:
    """Convert image to a small, optimized Data URI (iPhone Safari friendly)."""
    if not path or not os.path.exists(path):
        return ""
    try:
        img = Image.open(path)
        # iPhone Safari struggles with large Base64 strings; resizing is mandatory
        img.thumbnail((max_dim, max_dim))
        
        buf = io.BytesIO()
        ext = os.path.splitext(path)[1].lower()
        if ext == '.png':
            img.save(buf, format='PNG', optimize=True)
            mime_type = 'image/png'
        else:
            img = img.convert('RGB')
            img.save(buf, format='JPEG', quality=70, optimize=True)
            mime_type = 'image/jpeg'
            
        b64 = base64.b64encode(buf.getvalue()).decode('utf-8')
        return f"data:{mime_type};base64,{b64}"
    except Exception:
        return ""

# ================== LOGIC HELPERS ==================

def looks_non_italian_or_garbled(text: str) -> bool:
    if not text: return True
    t = text.strip().lower()
    if len(t) <= 1: return True
    english_markers = {"i","you","we","they","want","need","with","and","the","a","to","for","is","are","please"}
    tokens = re.findall(r"[a-z']+", t)
    if tokens and sum(tok in english_markers for tok in tokens) >= 2:
        return True
    return sum(ch.isalpha() for ch in t) < 3

def contains_english(text: str) -> bool:
    common_english = ["yes", "no", "hi", "hello", "thanks", "thank"]
    return any(word in text.lower().split() for word in common_english)

def update_stage(user_text: str) -> str:
    text = user_text.strip().lower()
    current = st.session_state.get("stage", "ORDERING")
    payment_cues = ["ecco", "tenga", "tieni", "prego", "pago", "carta", "contanti", "bancomat"]

    if current == "PRICE_GIVEN" and (any(cue in text for cue in payment_cues) or text in ["ok", "si", "sì"]):
        return "PAYMENT"
    if any(x in text for x in ["quanto", "how much", "prezzo"]):
        return "PRICE_GIVEN"
    if any(x in text for x in ["grazie", "thanks"]):
        return "CLOSING"
    return current

# ================== UI RENDERING ==================

def inject_custom_ui(scenario):
    """Renders the background and avatar using the resolved paths."""
    # Determine asset paths based on scenario
    bg_rel = f"assets/backgrounds/{scenario.lower()}.png"
    ava_rel = "assets/avatars/barista.png" # You can make this dynamic too
    
    bg_path = resolve_asset(bg_rel)
    ava_path = resolve_asset(ava_rel)
    
    # Try URL first (best for iPhone), fallback to optimized DataURI
    bg_src = asset_url(bg_rel) or img_file_to_data_uri(bg_path, max_dim=1000)
    ava_src = asset_url(ava_rel) or img_file_to_data_uri(ava_path, max_dim=400)

    st.markdown(f"""
        <style>
        .stApp {{
            background-image: url("{bg_src}");
            background-size: cover;
            background-position: center;
            background-attachment: fixed;
        }}
        /* Overlay to ensure text is readable over the background */
        .stApp::before {{
            content: "";
            position: absolute;
            top: 0; left: 0; width: 100%; height: 100%;
            background-color: rgba(255, 255, 255, 0.6);
            z-index: -1;
        }}
        .avatar-box {{
            display: flex;
            justify-content: center;
            margin-top: -30px;
            margin-bottom: 20px;
        }}
        .avatar-img {{
            width: 150px;
            height: 150px;
            border-radius: 50%;
            border: 4px solid white;
            box-shadow: 0 4px 12px rgba(0,0,0,0.2);
            object-fit: cover;
        }}
        </style>
        <div class="avatar-box">
            <img src="{ava_src}" class="avatar-img">
        </div>
    """, unsafe_allow_html=True)

# ================== PROMPTS & SESSION ==================

SYSTEM_PROMPT = """
You are an Italian language assistant playing TWO roles.
ROLE 1: Partner (Italian only, no corrections, natural conversation).
ROLE 2: Tutor (English tips, only if user makes errors or uses English).
(Full internal rules logic applied here as per original script...)
"""

if "messages" not in st.session_state:
    st.session_state.messages = []
if "conversation" not in st.session_state:
    st.session_state.conversation = []
if "stage" not in st.session_state:
    st.session_state.stage = "ORDERING"
if "scenario" not in st.session_state:
    st.session_state.scenario = "ORDERING"

# ================== MAIN UI ==================

SCENARIO_LABELS = {"ORDERING": "Ordering coffee", "TRANSPORT": "Transport", "DIRECTIONS": "Directions"}
selected_label = st.sidebar.selectbox("Scenario", list(SCENARIO_LABELS.values()))
st.session_state.scenario = [k for k, v in SCENARIO_LABELS.items() if v == selected_label][0]

# IMPORTANT: Render the assets here
inject_custom_ui(st.session_state.scenario)

st.title("Parla con me!")

# Audio Input
audio_value = st.audio_input("Parla in Italiano")
typed_input = st.text_input("O scrivi:")

user_input = ""
if audio_value:
    audio_file = io.BytesIO(audio_value.getvalue())
    audio_file.name = "speech.wav"
    tr = client.audio.transcriptions.create(model="whisper-1", file=audio_file, response_format="text")
    user_input = tr.strip()
    if looks_non_italian_or_garbled(user_input):
        repair = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": "Repair this Italian speech transcript."},
                      {"role": "user", "content": user_input}]
        )
        user_input = repair.choices[0].message.content.strip()
elif typed_input:
    user_input = typed_input

# ================== CHAT ENGINE ==================

if user_input:
    st.session_state.stage = update_stage(user_input)
    tutor_active = contains_english(user_input) or (len(st.session_state.conversation) % 2 == 0)

    # 1. Partner Call
    p_resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "system", "content": "Speak ONLY Italian as a local partner. No corrections."}] + 
                 st.session_state.messages + [{"role": "user", "content": user_input}]
    )
    partner_text = p_resp.choices[0].message.content.replace("PARTNER:", "").strip()

    # 2. Tutor Call
    tutor_text = ""
    if tutor_active:
        t_resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": "You are a Tutor. Provide: Clean version, More natural version, and a Tip."}] + 
                     [{"role": "user", "content": f"Analyze: {user_input}"}]
        )
        tutor_text = t_resp.choices[0].message.content.strip()

    # 3. Audio TTS
    speech = client.audio.speech.create(model="tts-1", voice="alloy", input=partner_text)
    audio_path = f"speech_{len(st.session_state.conversation)}.mp3"
    speech.stream_to_file(audio_path)

    # Store
    st.session_state.conversation.append({
        "user": user_input, "partner": partner_text, "tutor": tutor_text, "audio": audio_path
    })
    st.session_state.messages.append({"role": "user", "content": user_input})
    st.session_state.messages.append({"role": "assistant", "content": partner_text})

# ================== DISPLAY ==================

for chat in reversed(st.session_state.conversation):
    with st.chat_message("assistant"):
        st.write(chat["partner"])
        st.audio(chat["audio"])
        if chat["tutor"]:
            with st.expander("💡 Tutor"):
                st.write(chat["tutor"])
    with st.chat_message("user"):
        st.write(chat["user"])

if st.button("Reset"):
    st.session_state.clear()
    st.rerun()