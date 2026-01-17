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

# For iPhone stability, use a URL if possible. 
# Example: https://raw.githubusercontent.com/username/repo/main/
ASSET_BASE_URL = st.secrets.get('ASSET_BASE_URL', '').strip()
BASE_DIR = Path(__file__).resolve().parent

with open("vocab.json", encoding="utf-8") as f:
    vocab = json.load(f)["words"]

# ================== ASSET & IMAGE HELPERS ==================

def asset_url(rel_path: str) -> str | None:
    """Return an absolute URL for an asset if ASSET_BASE_URL is set."""
    if not rel_path or not ASSET_BASE_URL:
        return None
    base = ASSET_BASE_URL if ASSET_BASE_URL.endswith('/') else ASSET_BASE_URL + '/'
    return base + rel_path.lstrip('/')

def resolve_asset(path: str) -> str | None:
    """Resolve asset path with extension fallbacks."""
    cand = BASE_DIR / path
    if cand.exists(): return str(cand)
    # Fallback to common extensions
    for ext in ['.png', '.jpg', '.jpeg']:
        alt = cand.with_suffix(ext)
        if alt.exists(): return str(alt)
    return None

def img_file_to_data_uri(path: str, max_dim: int = 600) -> str:
    """Convert image to a small, compressed Data URI for iPhone Safari."""
    if not path or not os.path.exists(path):
        return ""
    try:
        img = Image.open(path)
        # Resize aggressively for mobile performance
        img.thumbnail((max_dim, max_dim))
        
        buf = io.BytesIO()
        # Use JPEG for backgrounds to save space, PNG for avatars with transparency
        if "avatar" in path.lower() or path.endswith('.png'):
            img.save(buf, format='PNG', optimize=True)
            m_type = 'image/png'
        else:
            img = img.convert('RGB')
            img.save(buf, format='JPEG', quality=60, optimize=True)
            m_type = 'image/jpeg'
            
        b64 = base64.b64encode(buf.getvalue()).decode('utf-8')
        return f"data:{m_type};base64,{b64}"
    except Exception:
        return ""

# ================== UI RENDERING ENGINE ==================

def render_ui_assets(scenario):
    """Injects CSS and HTML for background and avatar."""
    # Mapping scenarios to asset folders
    bg_filename = f"assets/backgrounds/{scenario.lower()}.png"
    ava_filename = f"assets/avatars/tutor.png" # or scenario specific
    
    bg_path = resolve_asset(bg_filename)
    ava_path = resolve_asset(ava_filename)

    # Prioritize remote URL (fastest/most stable on iPhone) -> then local DataURI
    bg_uri = asset_url(bg_filename) or img_file_to_data_uri(bg_path, max_dim=800)
    ava_uri = asset_url(ava_filename) or img_file_to_data_uri(ava_path, max_dim=300)

    st.markdown(f"""
        <style>
        .stApp {{
            background-image: url("{bg_uri}");
            background-size: cover;
            background-position: center;
            background-attachment: fixed;
        }}
        .stApp::before {{
            content: "";
            position: absolute;
            top: 0; left: 0; width: 100%; height: 100%;
            background-color: rgba(255, 255, 255, 0.5); /* Overlay to keep text readable */
            z-index: -1;
        }}
        .avatar-container {{
            display: flex;
            justify-content: center;
            padding: 20px 0;
        }}
        .avatar-img {{
            width: 120px;
            height: 120px;
            border-radius: 50%;
            border: 4px solid white;
            box-shadow: 0 4px 15px rgba(0,0,0,0.2);
            object-fit: cover;
            background-color: white;
        }}
        </style>
        <div class="avatar-container">
            <img src="{ava_uri}" class="avatar-img" alt="Partner">
        </div>
    """, unsafe_allow_html=True)

# ================== LOGIC HELPERS ==================

def looks_non_italian_or_garbled(text: str) -> bool:
    if not text or len(text.strip()) <= 1: return True
    english_markers = {"i","you","we","they","want","need","with","and","the","a","to","for"}
    tokens = re.findall(r"[a-z']+", text.lower())
    return sum(tok in english_markers for tok in tokens) >= 2

def update_stage(user_text: str) -> str:
    text = user_text.lower()
    if any(x in text for x in ["quanto", "how much", "prezzo"]): return "PRICE_GIVEN"
    if any(x in text for x in ["ecco", "pago", "carta", "contanti"]): return "PAYMENT"
    if any(x in text for x in ["grazie", "ciao", "arrivederci"]): return "CLOSING"
    return st.session_state.get("stage", "ORDERING")

# ================== SESSION STATE ==================
if "messages" not in st.session_state:
    st.session_state.messages = []
if "conversation" not in st.session_state:
    st.session_state.conversation = []
if "stage" not in st.session_state:
    st.session_state.stage = "ORDERING"
if "scenario" not in st.session_state:
    st.session_state.scenario = "ORDERING"

# ================== UI LAYOUT ==================
SCENARIO_LABELS = {"ORDERING": "Café", "TRANSPORT": "Station", "DIRECTIONS": "Street"}
scenario_key = st.sidebar.selectbox("Scenario", list(SCENARIO_LABELS.keys()), format_func=lambda x: SCENARIO_LABELS[x])
st.session_state.scenario = scenario_key

# RENDER BACKGROUND AND AVATAR
render_ui_assets(st.session_state.scenario)

st.title("Italian Roleplay")

# ================== INPUT HANDLING ==================
audio_value = st.audio_input("Parlami...")
typed_input = st.text_input("Oppure scrivi qui:")

user_input = ""
if audio_value:
    with st.spinner("Ascoltando..."):
        # Whisper Transcription
        tr = client.audio.transcriptions.create(
            model="whisper-1",
            file=("speech.wav", audio_value.getvalue()),
            prompt="Italian language learner.",
            response_format="text"
        )
        user_input = tr.strip()
        
        # Repair if garbled
        if looks_non_italian_or_garbled(user_input):
            repair = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "system", "content": "Repair this Italian transcript. Only return the Italian sentence."},
                          {"role": "user", "content": user_input}]
            )
            user_input = repair.choices[0].message.content.strip()
elif typed_input:
    user_input = typed_input

# ================== PROCESSING ==================
if user_input:
    st.session_state.stage = update_stage(user_input)
    
    # Simple logic to determine Tutor response (English words = Tutor active)
    tutor_active = any(word in user_input.lower() for word in ["yes", "no", "hello", "how"]) or len(st.session_state.conversation) % 2 == 0

    # Partner Call
    partner_resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "system", "content": "You are a helpful Italian local. Speak ONLY Italian. Never correct the user."}] + 
                 st.session_state.messages + [{"role": "user", "content": user_input}]
    )
    partner_text = partner_resp.choices[0].message.content.strip()

    # Tutor Call
    tutor_text = ""
    if tutor_active:
        tutor_resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": "You are a Tutor. Provide: Clean version, More natural version, and a Tip in English."},
                      {"role": "user", "content": f"Analyze: {user_input}"}]
        )
        tutor_text = tutor_resp.choices[0].message.content.strip()

    # Audio TTS for Partner
    speech_path = None
    try:
        speech = client.audio.speech.create(model="tts-1", voice="alloy", input=partner_text)
        speech_path = f"speech_{len(st.session_state.conversation)}.mp3"
        speech.stream_to_file(speech_path)
    except: pass

    st.session_state.conversation.append({
        "user": user_input, "partner": partner_text, "tutor": tutor_text, "audio": speech_path
    })
    st.session_state.messages.append({"role": "user", "content": user_input})
    st.session_state.messages.append({"role": "assistant", "content": partner_text})

# ================== DISPLAY CONVERSATION ==================
for chat in reversed(st.session_state.conversation):
    with st.chat_message("assistant"):
        st.write(chat["partner"])
        if chat["audio"]: st.audio(chat["audio"])
        if chat["tutor"]:
            with st.expander("💡 Tutor Feedback"):
                st.info(chat["tutor"])
    with st.chat_message("user"):
        st.write(chat["user"])

if st.button("Reset"):
    st.session_state.clear()
    st.rerun()