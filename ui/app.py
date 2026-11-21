"""Helix Knowledge Assistant: the chat window and the agent activity panel.

    make run-api     # the API, in one terminal
    make run-ui      # this app, on http://localhost:8501

or `make up` to run both in Docker.

Signing in: the first page is a sign-in form. The username and password go
to Keycloak (ui/auth.py), and the access token that comes back is sent to
the API with every request. The page then asks the API who the user is
(`GET /me`) and shows their name, role and the tools the role may use.
Tools the role cannot use are not shown at all; the API refuses them
anyway. Signing out (or a token the API no longer accepts) clears the
tokens and the conversations of this tab.

How a question flows:

1. The question is sent to `POST /chat` with the conversation's thread ID
   (none for the first question) and the user's access token.
2. The answer streams back as server-sent events. Each event updates the
   `Turn` (ui/conversation.py), and the turn is drawn again: tokens go to
   the chat window, everything else to the activity panel.
3. When the stream ends, the page is drawn once more from the saved state.

While an answer streams, the chat box turns into a Stop button. Stop (or
any other click) ends this script run; the stream is closed, the API sees
the client leave and stops the agents, and the next run marks the turn
as stopped.

Streamlit runs this file from top to bottom on every click, so all state
lives in `st.session_state`: the tokens, the user's profile and the
conversations of this browser tab. Session state stays on the UI server;
the tokens never reach the browser.
"""

from contextlib import closing, suppress

import streamlit as st
from streamlit.delta_generator import DeltaGenerator

from ui import views
from ui.auth import SESSION_ENDED, KeycloakLogin, LoginError, Session
from ui.client import ApiError, Budget, ChatClient, Profile, SavedThread, StreamOpened
from ui.config import MAX_QUESTION_CHARS, UiSettings
from ui.conversation import Conversation, Turn, TurnStatus
from ui.format import describe_wait
from ui.sse import KeepAlive

EXAMPLE_QUESTIONS = (
    "The PayHub database connection pool is exhausted. What should I do?",
    "What are the payment cut-off times?",
    "How do we roll back a bad configuration change?",
)

st.set_page_config(
    page_title="Helix Knowledge Assistant",
    page_icon=":material/forum:",
    layout="wide",
)


# Everything that belongs to the signed-in user. All of it is removed on
# sign-out, so the next person to use this tab sees none of it.
_USER_STATE = (
    "session",
    "profile",
    "conversations",
    "current",
    "pending",
    "saved_threads",
    "remembered",
    "open_saved",
    "forget_memory",
    "approval_answer",
    "feedback_request",
)


# ---------------------------------------------------------------
# Signing in and out
# ---------------------------------------------------------------


def _profile() -> Profile | None:
    profile = st.session_state.get("profile")
    return profile if isinstance(profile, Profile) else None


def _sign_out(login: KeycloakLogin, notice: str | None = None) -> None:
    session = st.session_state.get("session")
    if isinstance(session, Session):
        login.sign_out(session)
    for key in _USER_STATE:
        st.session_state.pop(key, None)
    if notice:
        st.session_state.notice = notice


def _access_token(login: KeycloakLogin) -> str:
    """A valid access token, refreshed first if it is about to expire.

    If the session cannot be refreshed, the user is signed out and the page
    starts again at the sign-in form.
    """
    session = st.session_state.get("session")
    try:
        if not isinstance(session, Session):
            raise LoginError(SESSION_ENDED)
        session = login.fresh(session)
    except LoginError as error:
        _sign_out(login, error.message)
        st.rerun()
    st.session_state.session = session
    return session.access_token


def _sign_in_page(login: KeycloakLogin, client: ChatClient) -> None:
    st.title("Helix Knowledge Assistant")
    if notice := st.session_state.pop("notice", None):
        st.info(notice, icon=":material/info:")
    _, middle, _ = st.columns([1, 2, 1])
    with middle, st.form("sign-in"):
        st.subheader("Sign in")
        username = st.text_input("Username", autocomplete="username")
        password = st.text_input("Password", type="password", autocomplete="current-password")
        if not st.form_submit_button("Sign in", type="primary", width="stretch"):
            return
        try:
            session = login.sign_in(username, password)
            profile = client.me(session.access_token)
        except LoginError as error:
            st.error(error.message, icon=":material/error:")
            return
        except ApiError as error:
            st.error(error.message, icon=":material/error:")
            return
    st.session_state.session = session
    st.session_state.profile = profile
    st.rerun()


# ---------------------------------------------------------------
# State: the conversations of this browser tab
# ---------------------------------------------------------------


def _conversations() -> list[Conversation]:
    state = st.session_state
    if "conversations" not in state:
        profile = _profile()
        first = Conversation(role=profile.role if profile else "")
        state.conversations = [first]
        state.current = first.key
    conversations: list[Conversation] = state.conversations
    return conversations


def _current() -> Conversation:
    conversations = _conversations()
    for conversation in conversations:
        if conversation.key == st.session_state.current:
            return conversation
    return conversations[-1]


# Callbacks run before the page is drawn again.


def _new_conversation() -> None:
    if not _current().turns:  # already empty: just use it
        return
    profile = _profile()
    fresh = Conversation(role=profile.role if profile else "")
    st.session_state.conversations.append(fresh)
    st.session_state.current = fresh.key


def _open(key: str) -> None:
    st.session_state.current = key


def _ask_example(question: str) -> None:
    st.session_state.pending = question


# ---------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------


@st.cache_data(ttl=10, show_spinner=False)
def _api_is_up(api_url: str) -> bool:
    return ChatClient(UiSettings(api_url=api_url)).is_up()


def _earlier_conversations() -> None:
    """Conversations saved on the server that are not open in this tab."""
    open_ids = {c.thread_id for c in _conversations() if c.thread_id}
    saved: list[SavedThread] = st.session_state.get("saved_threads") or []
    earlier = [t for t in saved if t.thread_id not in open_ids]
    if earlier:
        st.caption("Earlier conversations")
        for thread in earlier[:10]:
            st.button(
                thread.title or "Untitled",
                key=f"saved-{thread.thread_id}",
                on_click=_ask_to_open_saved,
                args=(thread.thread_id, thread.title),
                icon=":material/history:",
                width="stretch",
            )


def _ask_to_open_saved(thread_id: str, title: str) -> None:
    st.session_state.open_saved = (thread_id, title)


def _open_saved(client: ChatClient, token: str, thread_id: str) -> None:
    """Load a saved conversation from the server and show it in this tab."""
    try:
        messages = client.thread(token, thread_id)
    except ApiError as error:
        st.session_state.notice_error = error.message
        return
    profile = _profile()
    conversation = Conversation(role=profile.role if profile else "", thread_id=thread_id)
    question: str | None = None
    for role, text in messages:
        if role == "user":
            question = text
        elif question is not None:
            conversation.turns.append(
                Turn(
                    question=question,
                    status=TurnStatus.DONE,
                    final={"type": "response_end", "kind": "answered", "text": text},
                )
            )
            question = None
    st.session_state.conversations.append(conversation)
    st.session_state.current = conversation.key


def _refresh_saved(client: ChatClient, token: str) -> None:
    with suppress(ApiError):
        st.session_state.saved_threads = client.threads(token)


def _show_memory() -> None:
    st.session_state.remembered = "load"


def _ask_to_forget() -> None:
    st.session_state.forget_memory = True


def _memory_section() -> None:
    """What the assistant remembers about the user, and a way to clear it."""
    with st.expander("What I remember"):
        remembered = st.session_state.get("remembered")
        if not isinstance(remembered, list):
            st.caption("Past questions are remembered to help with later ones.")
            st.button("Show", key="show-memory", on_click=_show_memory)
            return
        if not remembered:
            st.caption("Nothing yet.")
            return
        for line in remembered[:10]:
            st.caption(f"- {line}")
        st.button(
            "Forget everything",
            key="forget-memory",
            icon=":material/delete:",
            on_click=_ask_to_forget,
        )


def _answer_approval(client: ChatClient, token: str, approval_id: str, approve: bool) -> None:
    """Send the user's answer to a waiting run, and stream the rest into the same turn."""
    for conversation in _conversations():
        for turn in conversation.turns:
            if turn.status is TurnStatus.WAITING and conversation.thread_id == approval_id:
                turn.status, turn.approval, turn.preview = TurnStatus.STREAMING, None, ""
                try:
                    with (
                        st.spinner("Working on it..." if approve else "Answering without it..."),
                        closing(
                            client.approve(approval_id, approve=approve, token=token)
                        ) as stream,
                    ):
                        for item in stream:
                            if isinstance(item, StreamOpened):
                                turn.run_id, turn.request_id = item.run_id, item.request_id
                            elif not isinstance(item, KeepAlive):
                                turn.apply(item)
                    turn.end_of_stream()
                except ApiError as error:
                    turn.fail(error.message)
                return


def _send_feedback(client: ChatClient, token: str, run_id: str, score: str) -> None:
    for conversation in _conversations():
        for turn in conversation.turns:
            if turn.run_id == run_id and conversation.thread_id:
                try:
                    client.feedback(
                        token=token, run_id=run_id, thread_id=conversation.thread_id, score=score
                    )
                    turn.feedback = score
                except ApiError as error:
                    st.session_state.notice_error = error.message
                return


def _memory_actions(client: ChatClient, token: str) -> None:
    """Carry out what the memory buttons asked for (they need the access token)."""
    if st.session_state.pop("forget_memory", False):
        with suppress(ApiError):
            client.forget(token)
        st.session_state.remembered = []
    if st.session_state.get("remembered") == "load":
        try:
            st.session_state.remembered = client.memory(token)
        except ApiError:
            st.session_state.remembered = []


def _account(profile: Profile, login: KeycloakLogin) -> None:
    """Who is signed in, and what their role lets them use.

    Only the allowed tools are listed: a viewer is not shown the analysis
    or admin tools at all. The API checks every request itself, so this
    list is for the user's benefit, not a security control.
    """
    st.markdown(f"**{profile.name}**")
    st.caption(f"`{profile.username}` · Role: **{profile.role}**")
    with st.expander("What you can use"):
        for permission in profile.allowed:
            st.markdown(f":material/check: {permission.label}")
        st.caption("Documents you can read: " + ", ".join(profile.access_levels))
    if profile.budget is not None:
        _budget(profile.budget)
    _memory_section()
    st.button(
        "Sign out", key="sign-out", icon=":material/logout:", on_click=_sign_out, args=(login,)
    )


def _budget(budget: Budget) -> None:
    """Today's token use, so the daily limit never comes as a surprise."""
    st.progress(
        budget.share_used,
        text=f"Today's usage: {budget.used:,} of {budget.limit:,} tokens",
    )
    if budget.used >= budget.limit:
        st.caption(f"The budget resets at 00:00 UTC, in {describe_wait(budget.resets_in_seconds)}.")


def _refresh_profile(client: ChatClient, token: str) -> None:
    """Read the profile again after a question, so the usage shown is current.

    Best effort: if it fails, the last profile stays on screen.
    """
    with suppress(ApiError):
        st.session_state.profile = client.me(token)


def _sidebar(
    settings: UiSettings, current: Conversation, profile: Profile, login: KeycloakLogin
) -> None:
    with st.sidebar:
        _account(profile, login)
        st.divider()
        st.button(
            "New conversation",
            key="new-conversation",
            icon=":material/add:",
            on_click=_new_conversation,
            width="stretch",
        )

        st.markdown("**Conversations**")
        for conversation in reversed(_conversations()):
            st.button(
                conversation.title,
                key=f"open-{conversation.key}",
                on_click=_open,
                args=(conversation.key,),
                type="primary" if conversation.key == current.key else "secondary",
                width="stretch",
            )
        _earlier_conversations()

        st.divider()
        if _api_is_up(settings.api_url):
            st.caption(f":green[:material/circle:] API connected: {settings.api_url}")
        else:
            st.caption(f":red[:material/circle:] API not reachable: {settings.api_url}")
        if current.thread_id:
            st.caption(f"Thread `{current.thread_id}`")


# ---------------------------------------------------------------
# Asking a question and drawing the answer as it streams
# ---------------------------------------------------------------


def _ask(
    client: ChatClient,
    token: str,
    conversation: Conversation,
    question: str,
    answer_slot: DeltaGenerator,
    panel_slot: DeltaGenerator,
) -> bool:
    """Ask the question and draw the answer as it arrives.

    Returns False when the API no longer accepts the login token.
    """
    turn = Turn(question=question)
    conversation.turns.append(turn)

    def draw_answer() -> None:
        with answer_slot.container():
            views.answer(turn, controls=False)

    def draw_all() -> None:
        draw_answer()
        with panel_slot.container():
            views.activity(turn)

    draw_all()
    try:
        stream = client.ask(question, token=token, thread_id=conversation.thread_id)
        with closing(stream):
            for item in stream:
                if isinstance(item, StreamOpened):
                    conversation.thread_id = item.thread_id
                    turn.run_id, turn.request_id = item.run_id, item.request_id
                elif isinstance(item, KeepAlive):
                    # Nothing new, but drawing lets Streamlit notice a Stop click.
                    draw_answer()
                else:
                    turn.apply(item)
                    if item["type"] == "token":
                        draw_answer()
                    else:
                        draw_all()
        turn.end_of_stream()
    except ApiError as error:
        turn.fail(error.message)
        turn.request_id = error.request_id or turn.request_id
        if error.status == 401:
            return False
    draw_all()
    return True


def _welcome() -> None:
    st.markdown(
        "Ask about Helix Bank's policies, runbooks, incident reports and system "
        "designs. Every answer lists its sources, and the panel on the right shows "
        "what the agents did to get it."
    )
    for number, question in enumerate(EXAMPLE_QUESTIONS):
        st.button(question, key=f"example-{number}", on_click=_ask_example, args=(question,))


def _history(conversation: Conversation) -> None:
    for turn in conversation.turns:
        with st.chat_message("user"):
            st.markdown(turn.question)
        with st.chat_message("assistant"):
            views.answer(turn)


def _panel_for(conversation: Conversation) -> Turn | None:
    """The turn whose activity the panel shows. The latest one by default."""
    turns = conversation.turns
    if not turns:
        return None
    if len(turns) == 1:
        return turns[0]
    number = st.selectbox(
        "Question",
        range(len(turns)),
        index=len(turns) - 1,
        format_func=lambda n: f"{n + 1}. {turns[n].question[:60]}",
        key=f"panel-{conversation.key}",
    )
    return turns[number if number is not None else -1]


def main() -> None:
    settings = UiSettings.from_env()
    client = ChatClient(settings)
    login = KeycloakLogin(settings)

    profile = _profile()
    if profile is None:
        _sign_in_page(login, client)
        return

    # A turn still marked as streaming belongs to a run that was stopped:
    # the user pressed Stop, or clicked something while it streamed.
    for conversation in _conversations():
        for turn in conversation.turns:
            if turn.status is TurnStatus.STREAMING:
                turn.stop()

    # Work that needs the access token: loading a saved conversation, the
    # memory buttons, and the list of saved conversations (once per sign-in).
    if (wanted := st.session_state.pop("open_saved", None)) is not None:
        _open_saved(client, _access_token(login), wanted[0])
    if st.session_state.get("forget_memory") or st.session_state.get("remembered") == "load":
        _memory_actions(client, _access_token(login))
    if "saved_threads" not in st.session_state:
        _refresh_saved(client, _access_token(login))
    if (answer := st.session_state.pop("approval_answer", None)) is not None:
        _answer_approval(client, _access_token(login), answer[0], answer[1])
    if (request := st.session_state.pop("feedback_request", None)) is not None:
        _send_feedback(client, _access_token(login), request[0], request[1])

    current = _current()
    _sidebar(settings, current, profile, login)
    if notice := st.session_state.pop("notice_error", None):
        st.error(notice, icon=":material/error:")

    question = st.chat_input(
        "Ask a question about Helix Bank's documents",
        max_chars=MAX_QUESTION_CHARS,
        submit_mode="stop",
    )
    question = question or st.session_state.pop("pending", None)
    if isinstance(question, str) and not question.strip():
        question = None

    st.title("Helix Knowledge Assistant")
    chat, panel = st.columns([3, 2], gap="large")

    with chat:
        if not current.turns and not question:
            _welcome()
        _history(current)

    with panel:
        st.subheader("Agent activity")
        if question:
            panel_slot = st.empty()
        else:
            shown = _panel_for(current)
            if shown is None:
                st.caption("The agents' steps appear here while they answer.")
            else:
                views.activity(shown)

    if question:
        with chat:
            with st.chat_message("user"):
                st.markdown(question)
            with st.chat_message("assistant"):
                answer_slot = st.empty()
        token = _access_token(login)
        if _ask(client, token, current, str(question), answer_slot, panel_slot):
            _refresh_profile(client, token)
            _refresh_saved(client, token)
        else:
            _sign_out(login, SESSION_ENDED)
        # Draw the page again from the saved state: history, panel picker
        # and an enabled chat box.
        st.rerun()


main()
