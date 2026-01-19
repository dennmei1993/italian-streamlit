
import streamlit as st

st.set_page_config(page_title="Language Conversation Tutor", layout="mobile")

# ============================
# Session state init
# ============================
st.session_state.setdefault("page", "home")
st.session_state.setdefault("conversation_log", [])
st.session_state.setdefault("active_interaction", None)
st.session_state.setdefault("last_user_input", "")

# ============================
# Helpers
# ============================
def reset_conversation_state(clear_log=True):
    if clear_log:
        st.session_state.conversation_log = []
    st.session_state.active_interaction = None
    st.session_state.last_user_input = ""

# ============================
# Navigation (query params)
# ============================
params = st.query_params
action = params.get("action")

if action == "home":
    reset_conversation_state(clear_log=False)
    st.session_state.page = "home"
    st.query_params.clear()
    st.rerun()

elif action == "new":
    reset_conversation_state(clear_log=True)
    st.session_state.page = "conversation"
    st.query_params.clear()
    st.rerun()

elif action == "end":
    st.session_state.page = "review"
    st.query_params.clear()
    st.rerun()

# ============================
# HOME PAGE
# ============================
if st.session_state.page == "home":
    st.markdown("## Language Conversation Tutor")
    st.write("Choose a scenario and start practising.")
    if st.button("Start Conversation"):
        reset_conversation_state(clear_log=True)
        st.session_state.page = "conversation"
        st.rerun()

# ============================
# CONVERSATION PAGE
# ============================
elif st.session_state.page == "conversation":
    st.markdown("## Conversation")

    # Scenario panel nav (HTML, safe)
    st.markdown("""
    <div style="display:flex;justify-content:center;gap:12px;margin-bottom:12px">
      <a href="?action=home">Home</a>
      <a href="?action=new">New</a>
      <a href="?action=end">End</a>
    </div>
    """, unsafe_allow_html=True)

    user_input = st.text_input("Say something in Italian")

    if user_input and user_input != st.session_state.last_user_input:
        st.session_state.last_user_input = user_input

        partner_text = f"(Italian reply to: {user_input})"
        tutor_tip = "Looks good 👍"

        # CRITICAL FIX: log immediately
        turn = {
            "user": user_input,
            "partner": partner_text,
            "tutor": tutor_tip,
        }
        st.session_state.conversation_log.append(turn)
        st.session_state.active_interaction = turn

    if st.session_state.active_interaction:
        t = st.session_state.active_interaction
        st.markdown(f"**You:** {t['user']}")
        st.markdown(f"**Partner:** {t['partner']}")
        st.markdown(f"**Tutor:** {t['tutor']}")

# ============================
# REVIEW PAGE
# ============================
elif st.session_state.page == "review":
    st.markdown("## Conversation Review")

    log = st.session_state.conversation_log
    if not log:
        st.warning("No conversation history recorded.")
    else:
        for i, t in enumerate(log, 1):
            st.markdown(f"### Turn {i}")
            st.markdown(f"**You:** {t['user']}")
            st.markdown(f"**Partner:** {t['partner']}")
            st.markdown(f"**Tutor:** {t['tutor']}")
