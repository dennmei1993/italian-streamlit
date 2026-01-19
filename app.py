import os
import re
import json
import time
import base64
import hashlib
import tempfile
from secrets import token_hex
from typing import Any, Dict, List, Optional

import streamlit as st

# Optional OpenAI dependency (your original app uses it).
# Keep this import; Streamlit Cloud will work if openai is in requirements.txt.
try:
    from openai import OpenAI
except Exception:
    OpenAI = None  # type: ignore


# =========================================================
# PAGE CONFIG
# =========================================================
st.set_page_config(page_title="Language Conversation Tutor", page_icon="🗣️", layout="centered")


# =========================================================
# QUERY PARAMS + SID
# =========================================================
def _get_query_param(key: str) -> Optional[str]:
    try:
        # New API
        val = st.query_params.get(key)
        if isinstance(val, list):
            return val[0] if val else None
        return str(val) if val is not None else None
    except Exception:
        # Legacy API
        try:
            qp = st.experimental_get_query_params()
            if key in qp and qp[key]:
                return qp[key][0]
        except Exception:
            pass
    return None


def _set_query_params(**kwargs: str) -> None:
    try:
        st.query_params.update(kwargs)
    except Exception:
        try:
            st.experimental_set_query_params(**kwargs)
        except Exception:
            pass


def _ensure_sid() -> str:
    sid = _get_query_param("sid")
    if sid:
        return sid
    sid = token_hex(8)
    _set_query_params(sid=sid)
    # rerun to ensure SID is available everywhere
    st.rerun()
    return sid  # pragma: no cover


SID = _ensure_sid()


# =========================================================
# DURABLE LOG PERSISTENCE (disk JSON keyed by SID)
# =========================================================
def _log_file_path(sid: str) -> str:
    # Streamlit Cloud allows writing to /tmp in the running container session
    return os.path.join(tempfile.gettempdir(), f"conversation_log_{sid}.json")


def persist_log_to_disk() -> None:
    """Persist session_state.conversation_log to disk."""
    try:
        path = _log_file_path(SID)
        log = st.session_state.get("conversation_log", [])
        with open(path, "w", encoding="utf-8") as f:
            json.dump(log, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def load_log_from_disk() -> List[Dict[str, Any]]:
    """Load conversation log for SID from disk."""
    try:
        path = _log_file_path(SID)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
    except Exception:
        pass
    return []


def clear_log_on_disk() -> None:
    try:
        path = _log_file_path(SID)
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


# =========================================================
# OPTIONAL VOCAB (used as hint for repair prompt)
# =========================================================
vocab: List[str] = []
try:
    with open("vocab.json", encoding="utf-8") as f:
        vocab = (json.load(f) or {}).get("words", []) or []
except Exception:
    vocab = []


# =========================================================
# SESSION STATE DEFAULTS
# =========================================================
def _init_state() -> None:
    st.session_state.setdefault("page", "home")  # home | conversation | review
    st.session_state.setdefault("scenario", "Ordering coffee / food")
    st.session_state.setdefault("show_tutor", True)
    st.session_state.setdefault("show_translation", True)
    st.session_state.setdefault("playback_my_sentence", True)

    # Chat messages for partner role (keeps context for LLM)
    st.session_state.setdefault("messages", [])

    # One "active interaction" shown on conversation page (cleared each new turn)
    st.session_state.setdefault("active_interaction", None)

    # Archived turns for Review
    st.session_state.setdefault("conversation_log", [])

    # Turn tracking
    st.session_state.setdefault("turn_count", 0)
    st.session_state.setdefault("last_user_input", "")
    st.session_state.setdefault("last_audio_hash", None)


_init_state()


# Hydrate log from disk if empty (critical for Review reliability)
if not st.session_state.conversation_log:
    disk_log = load_log_from_disk()
    if disk_log:
        st.session_state.conversation_log = list(disk_log)


# =========================================================
# RESET + ARCHIVE HELPERS
# =========================================================
def reset_conversation_state(clear_log: bool = True) -> None:
    """Reset the conversation. If clear_log, clear both memory and disk log."""
    st.session_state.messages = []
    st.session_state.turn_count = 0
    st.session_state.last_user_input = ""
    st.session_state.last_audio_hash = None
    st.session_state.active_interaction = None

    if clear_log:
        st.session_state.conversation_log = []
        clear_log_on_disk()
    else:
        # keep log
        persist_log_to_disk()


def archive_active_interaction() -> None:
    """
    Append current active_interaction to conversation_log exactly once,
    then persist to disk. This is the core of the Review fix.
    """
    turn = st.session_state.get("active_interaction")
    if not turn:
        return

    st.session_state.setdefault("conversation_log", [])
    log = st.session_state.conversation_log

    # prevent double-append on reruns
    if log and log[-1].get("turn_id") == turn.get("turn_id"):
        return
    if log and log[-1].get("user") == turn.get("user") and log[-1].get("partner") == turn.get("partner"):
        # additional safety
        return

    log.append(dict(turn))
    persist_log_to_disk()


# =========================================================
# OPENAI CLIENT + MODEL SETTINGS
# =========================================================
def _get_openai_client() -> Optional[Any]:
    if OpenAI is None:
        return None
    # Works with Streamlit secrets:
    # st.secrets["OPENAI_API_KEY"] = "..."
    api_key = None
    try:
        api_key = st.secrets.get("OPENAI_API_KEY")
    except Exception:
        api_key = None
    if not api_key:
        return None
    try:
        return OpenAI(api_key=api_key)
    except Exception:
        return None


CLIENT = _get_openai_client()

CHAT_MODEL = "gpt-4o-mini"  # reasonable default
WHISPER_MODEL = "whisper-1"
TTS_MODEL = "gpt-4o-mini-tts"
TTS_VOICE = "alloy"


# =========================================================
# TEXT HELPERS
# =========================================================
def looks_like_english(text: str) -> bool:
    if not text:
        return False
    # Heuristic: common English words / contractions
    english_markers = [" the ", " and ", " i ", " you ", " to ", " don't", "can't", " please "]
    t = " " + text.lower().strip() + " "
    return any(m in t for m in english_markers)


def looks_garbled(text: str) -> bool:
    if not text:
        return True
    t = text.strip()
    # too short or too many non-letters
    letters = sum(ch.isalpha() for ch in t)
    ratio = letters / max(1, len(t))
    if len(t) < 2:
        return True
    if ratio < 0.45:
        return True
    return False


def make_turn_id(audio_bytes: bytes) -> str:
    return hashlib.sha256(audio_bytes).hexdigest()[:16]


# =========================================================
# OPENAI CALLS
# =========================================================
def transcribe_audio(audio_bytes: bytes) -> str:
    if CLIENT is None:
        return ""
    try:
        # Whisper expects a file-like object; write temp file
        with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as f:
            f.write(audio_bytes)
            tmp_path = f.name

        with open(tmp_path, "rb") as f:
            result = CLIENT.audio.transcriptions.create(
                model=WHISPER_MODEL,
                file=f,
                prompt="Trascrivi in italiano. Se senti un accento da studente, prova comunque a rendere il testo in italiano naturale.",
            )
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        return (getattr(result, "text", "") or "").strip()
    except Exception:
        return ""


def repair_transcript_to_italian(text: str) -> str:
    """
    If transcript seems garbled or English-heavy, we repair it into a plausible Italian sentence.
    """
    if CLIENT is None:
        return text

    hint = ""
    if vocab:
        hint = f"\nVocabolario utile (solo come suggerimento): {', '.join(vocab[:40])}\n"

    prompt = (
        "You are helping a language learner. You will receive a rough transcript of what they said.\n"
        "If it's already good Italian, return it unchanged.\n"
        "If it's English or garbled, rewrite as a plausible Italian sentence that matches the intent.\n"
        "Return ONLY the Italian sentence.\n"
        f"{hint}\n"
        f"Transcript:\n{text}\n"
    )

    try:
        resp = CLIENT.chat.completions.create(
            model=CHAT_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
        )
        out = (resp.choices[0].message.content or "").strip()
        # Keep it one line
        out = re.sub(r"\s+", " ", out).strip()
        return out or text
    except Exception:
        return text


def partner_reply_it(messages: List[Dict[str, str]], scenario: str) -> str:
    if CLIENT is None:
        return ""

    system = (
        "You are a friendly conversation partner for a language learner.\n"
        "Rules:\n"
        "- Speak ONLY in Italian.\n"
        "- Do NOT correct the user.\n"
        "- Do NOT translate.\n"
        "- Keep replies natural, short-to-medium length.\n"
        "- Stay in the scenario.\n"
    )

    scenario_hint = f"Scenario: {scenario}. Keep the conversation consistent with this setting."
    full = [{"role": "system", "content": system}, {"role": "system", "content": scenario_hint}] + messages

    try:
        resp = CLIENT.chat.completions.create(
            model=CHAT_MODEL,
            messages=full,
            temperature=0.7,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception:
        return ""


def tutor_tip_en(user_text: str, partner_text: str) -> Dict[str, str]:
    """
    Returns {"raw": ..., "recommended": ..., "tip": ...}
    raw may be "Looks good 👍" or structured.
    """
    if CLIENT is None:
        return {"raw": "", "recommended": "", "tip": ""}

    prompt = (
        "You are an English tutor helping a learner of Italian.\n"
        "Given the user's Italian (or English) message and the partner's Italian reply:\n"
        "- If the user's sentence is already good Italian, respond exactly: Looks good 👍\n"
        "- Otherwise, output TWO lines:\n"
        "Recommended: <better Italian sentence>\n"
        "Tip: <short English explanation>\n\n"
        f"User:\n{user_text}\n\nPartner:\n{partner_text}\n"
    )

    try:
        resp = CLIENT.chat.completions.create(
            model=CHAT_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
        )
        raw = (resp.choices[0].message.content or "").strip()

        if raw.lower().startswith("looks good"):
            return {"raw": raw, "recommended": "", "tip": ""}

        rec = ""
        tip = ""
        for line in raw.splitlines():
            if line.lower().startswith("recommended:"):
                rec = line.split(":", 1)[1].strip()
            if line.lower().startswith("tip:"):
                tip = line.split(":", 1)[1].strip()

        return {"raw": raw, "recommended": rec, "tip": tip}
    except Exception:
        return {"raw": "", "recommended": "", "tip": ""}


def translate_to_english(it_text: str) -> str:
    if CLIENT is None:
        return ""
    prompt = "Translate the following Italian into natural English. Return only the English:\n\n" + it_text
    try:
        resp = CLIENT.chat.completions.create(
            model=CHAT_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception:
        return ""


def tts_to_file(text: str, suffix: str = "tts") -> Optional[str]:
    if CLIENT is None or not text:
        return None
    try:
        out_path = os.path.join(tempfile.gettempdir(), f"{suffix}_{SID}_{int(time.time()*1000)}.mp3")
        audio = CLIENT.audio.speech.create(
            model=TTS_MODEL,
            voice=TTS_VOICE,
            input=text,
        )
        with open(out_path, "wb") as f:
            f.write(audio.read())
        return out_path
    except Exception:
        return None


# =========================================================
# SCENARIO ASSETS (same structure as your current app)
# =========================================================
STAGE_BACKGROUNDS = {
    "Ordering coffee / food": "assets/backgrounds/cafe.jpg",
    "Buying tickets / transport": "assets/backgrounds/transport.jpg",
    "Asking directions": "assets/backgrounds/directions.jpg",
}
STAGE_AVATARS = {
    "Ordering coffee / food": "assets/avatars/barista.png",
    "Buying tickets / transport": "assets/avatars/ticket_clerk.png",
    "Asking directions": "assets/avatars/local_person.png",
}


def _img_to_data_uri(path: str) -> Optional[str]:
    try:
        if not os.path.exists(path):
            return None
        with open(path, "rb") as f:
            b = f.read()
        ext = os.path.splitext(path)[1].lower().replace(".", "")
        mime = "image/png" if ext == "png" else "image/jpeg"
        return f"data:{mime};base64,{base64.b64encode(b).decode('utf-8')}"
    except Exception:
        return None


def render_scene_panel(scenario: str) -> None:
    bg_path = STAGE_BACKGROUNDS.get(scenario, "")
    av_path = STAGE_AVATARS.get(scenario, "")

    bg_uri = _img_to_data_uri(bg_path) if bg_path else None
    av_uri = _img_to_data_uri(av_path) if av_path else None

    if not bg_uri:
        st.warning(f"Missing background image: {bg_path}")
        return

    avatar_html = ""
    if av_uri:
        avatar_html = f"""
        <img src="{av_uri}" style="
            position:absolute;
            right:18px;
            bottom:0px;
            width:140px;
            height:auto;
            filter: drop-shadow(0px 8px 16px rgba(0,0,0,0.45));
        " />
        """

    st.markdown(
        f"""
        <div style="
            position:relative;
            width:100%;
            height:220px;
            border-radius:18px;
            overflow:hidden;
            margin-bottom:10px;
            background-image:url('{bg_uri}');
            background-size:cover;
            background-position:center;
        ">
            <div style="
                position:absolute;
                inset:0;
                background: linear-gradient(180deg, rgba(0,0,0,0.10) 0%, rgba(0,0,0,0.55) 100%);
            "></div>
            <div style="
                position:absolute;
                left:16px;
                bottom:14px;
                color:white;
                font-size:18px;
                font-weight:600;
                text-shadow:0px 2px 12px rgba(0,0,0,0.6);
            ">
                {scenario}
            </div>
            {avatar_html}
        </div>
        """,
        unsafe_allow_html=True,
    )


# =========================================================
# NAV BAR (Streamlit buttons, reliable)
# =========================================================
def nav_bar() -> None:
    c1, c2, c3 = st.columns(3)
    with c1:
        if st.button("🏠 Home", use_container_width=True):
            st.session_state.page = "home"
            st.rerun()
    with c2:
        if st.button("🆕 New", use_container_width=True):
            reset_conversation_state(clear_log=True)
            st.session_state.page = "conversation"
            st.rerun()
    with c3:
        if st.button("⏹ Review", use_container_width=True):
    # Treat Review as "closing" the current turn
            archive_active_interaction()
            st.session_state.active_interaction = None
            persist_log_to_disk()
            st.session_state.page = "review"
            st.rerun()


# =========================================================
# HOME PAGE
# =========================================================
if st.session_state.page == "home":
    st.title("🗣️ Language Conversation Tutor")
    st.caption("Choose a scenario and settings, then start.")

    st.session_state.scenario = st.selectbox(
        "Choose a scenario",
        ["Ordering coffee / food", "Buying tickets / transport", "Asking directions"],
        index=["Ordering coffee / food", "Buying tickets / transport", "Asking directions"].index(st.session_state.scenario)
        if st.session_state.scenario in ["Ordering coffee / food", "Buying tickets / transport", "Asking directions"]
        else 0,
    )

    st.session_state.show_tutor = st.toggle("Show tutor tips", value=bool(st.session_state.show_tutor))
    st.session_state.show_translation = st.toggle("Enable translation", value=bool(st.session_state.show_translation))
    st.session_state.playback_my_sentence = st.toggle(
        "Play back my sentence (TTS)",
        value=bool(st.session_state.playback_my_sentence),
    )

    if CLIENT is None:
        st.warning("OpenAI API key not found. Add OPENAI_API_KEY to Streamlit Secrets to enable AI features.")

    if st.button("▶ Start", use_container_width=True):
        reset_conversation_state(clear_log=True)
        st.session_state.page = "conversation"
        st.rerun()

    st.stop()


# =========================================================
# REVIEW PAGE
# =========================================================
if st.session_state.page == "review":
    st.title("📜 Conversation Review")
    st.caption("Your full conversation history for this session.")

    # Always attempt to archive current on-screen interaction first
    archive_active_interaction()

    nav_bar()
    st.divider()

    # Robust load:
    log = st.session_state.get("conversation_log", []) or []
    if not log:
        disk_log = load_log_from_disk()
        if disk_log:
            st.session_state.conversation_log = list(disk_log)
            log = st.session_state.conversation_log

    if not log:
        st.info("No conversation history yet. Record a message in Conversation, then come back here.")
        st.stop()

    for i, turn in enumerate(log, start=1):
        st.markdown(f"### Turn {i}")

        user_text = (turn.get("user") or "").strip()
        partner_text = (turn.get("partner") or "").strip()

        st.markdown(f"**You:** {user_text}")
        user_audio = turn.get("user_audio")
        if user_audio and os.path.exists(user_audio):
            st.audio(user_audio)

        st.markdown(f"**Partner:** {partner_text}")
        partner_audio = turn.get("partner_audio")
        if partner_audio and os.path.exists(partner_audio):
            st.audio(partner_audio)

        trans = (turn.get("translation") or "").strip()
        if trans:
            st.markdown(f"**Translation:** {trans}")

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
                    rec_audio = turn.get("tutor_recommended_audio")
                    if rec_audio and os.path.exists(rec_audio):
                        st.audio(rec_audio)
                if tip:
                    st.markdown(f"**Tip:** {tip}")

        st.divider()

    st.stop()


# =========================================================
# CONVERSATION PAGE
# =========================================================
scenario = st.session_state.scenario
show_tutor = bool(st.session_state.show_tutor)
show_translation = bool(st.session_state.show_translation)
playback_my_sentence = bool(st.session_state.playback_my_sentence)

st.title("💬 Conversation")
nav_bar()

render_scene_panel(scenario)

# If OpenAI isn't configured, still show UI but disable processing
if CLIENT is None:
    st.info("Add OPENAI_API_KEY in Streamlit Secrets to enable transcription & AI conversation.")
    st.stop()

#st.markdown("### 🎙️ Speak")
audio = st.audio_input("Press the mic icon and speak")

def _bytes_from_audio_input(audio_obj: Any) -> Optional[bytes]:
    try:
        # audio_input returns an UploadedFile-like object
        return audio_obj.getvalue()
    except Exception:
        try:
            return audio_obj.read()
        except Exception:
            return None


if audio:
    audio_bytes = _bytes_from_audio_input(audio)
    if audio_bytes:
        audio_hash = make_turn_id(audio_bytes)

        # Prevent re-processing the exact same audio on rerun
        if st.session_state.last_audio_hash != audio_hash:
            # IMPORTANT FIX:
            # Archive the previous interaction BEFORE clearing for the new turn.
            archive_active_interaction()
            st.session_state.active_interaction = None

            st.session_state.last_audio_hash = audio_hash

            # Save user's audio to temp file so Review can play it back
            user_audio_path = os.path.join(tempfile.gettempdir(), f"user_{SID}_{audio_hash}.wav")
            try:
                with open(user_audio_path, "wb") as f:
                    f.write(audio_bytes)
            except Exception:
                user_audio_path = ""

            # Transcribe
            user_text_raw = transcribe_audio(audio_bytes)

            # Repair if needed
            repaired = ""
            user_text = user_text_raw
            if looks_garbled(user_text_raw) or looks_like_english(user_text_raw):
                repaired = repair_transcript_to_italian(user_text_raw)
                if repaired:
                    user_text = repaired

            # Update conversational context
            st.session_state.messages.append({"role": "user", "content": user_text})

            # Partner reply
            partner_text = partner_reply_it(st.session_state.messages, scenario)
            st.session_state.messages.append({"role": "assistant", "content": partner_text})

            # TTS audio (partner)
            partner_audio_path = tts_to_file(partner_text, suffix="partner") or ""

            # Tutor logic (optional)
            tutor = {"raw": "", "recommended": "", "tip": ""}
            tutor_audio_path = ""
            # Simple trigger: if English-ish or every 2 turns, show tutor
            st.session_state.turn_count += 1
            if show_tutor and (looks_like_english(user_text_raw) or st.session_state.turn_count % 2 == 0):
                tutor = tutor_tip_en(user_text, partner_text)
                if tutor.get("recommended"):
                    tutor_audio_path = tts_to_file(tutor["recommended"], suffix="tutor") or ""

            # Playback user sentence TTS (optional)
            my_sentence_audio_path = ""
            if playback_my_sentence:
                my_sentence_audio_path = tts_to_file(user_text, suffix="me") or ""

            # Build interaction
            st.session_state.active_interaction = {
                "turn_id": audio_hash,
                "user": user_text,
                "user_raw": user_text_raw,
                "repaired_as": repaired,
                "user_audio": user_audio_path,
                "my_sentence_audio": my_sentence_audio_path,
                "partner": partner_text,
                "partner_audio": partner_audio_path,
                "translation": "",
                "tutor_raw": tutor.get("raw", ""),
                "tutor_recommended": tutor.get("recommended", ""),
                "tutor_tip": tutor.get("tip", ""),
                "tutor_recommended_audio": tutor_audio_path,
                "timestamp": time.time(),
            }

            # Persist right away (so Review works even if user navigates immediately)
            persist_log_to_disk()

            st.rerun()

# ------------------ Interaction Panel ------------------
turn = st.session_state.get("active_interaction")

#st.markdown("### 🧩 Interaction")
if not turn:
    st.info("Record a message to start.")
    st.stop()

# Show repaired note if applicable
if (turn.get("repaired_as") or "").strip():
    st.caption(f"🛠️ Interpreted as: {turn.get('user')}")

st.markdown(f"**You:** {turn.get('user','')}")
if turn.get("user_audio") and os.path.exists(turn["user_audio"]):
    st.audio(turn["user_audio"])
if turn.get("my_sentence_audio") and os.path.exists(turn["my_sentence_audio"]):
    st.caption("🔊 Your sentence (TTS)")
    st.audio(turn["my_sentence_audio"])

st.markdown(f"**Partner:** {turn.get('partner','')}")
if turn.get("partner_audio") and os.path.exists(turn["partner_audio"]):
    st.audio(turn["partner_audio"])

if show_translation:
    if st.button("Translate partner reply", key="btn_translate"):
        trans = translate_to_english(turn.get("partner", ""))
        st.session_state.active_interaction["translation"] = trans
        # Persist updated turn to disk: archive/replace strategy
        # Replace last archived version if it matches this turn_id, otherwise just persist current state.
        # (We persist disk log on next archive; this ensures translate doesn't disappear.)
        persist_log_to_disk()
        st.rerun()

    trans = (turn.get("translation") or "").strip()
    if trans:
        st.markdown(f"**Translation:** {trans}")

if show_tutor and (turn.get("tutor_raw") or "").strip():
    st.markdown("### 🧑‍🏫 Tutor")
    raw = (turn.get("tutor_raw") or "").strip()
    if raw.lower().startswith("looks good"):
        st.success(raw)
    else:
        rec = (turn.get("tutor_recommended") or "").strip()
        tip = (turn.get("tutor_tip") or "").strip()
        if rec:
            st.markdown(f"**Recommended:** {rec}")
            if turn.get("tutor_recommended_audio") and os.path.exists(turn["tutor_recommended_audio"]):
                st.audio(turn["tutor_recommended_audio"])
        if tip:
            st.markdown(f"**Tip:** {tip}")

st.stop()
