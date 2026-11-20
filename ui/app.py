"""Helix Knowledge Assistant: the chat window and the agent activity panel.

    make run-api     # the API, in one terminal
    make run-ui      # this app, on http://localhost:8501

or `make up` to run both in Docker.

How a question flows:

1. The question is sent to `POST /chat` with the conversation's thread ID
   (none for the first question) and the role picked in the sidebar.
2. The answer streams back as server-sent events. Each event updates the
   `Turn` (ui/conversation.py), and the turn is drawn again: tokens go to
   the chat window, everything else to the activity panel.
3. When the stream ends, the page is drawn once more from the saved state.

While an answer streams, the chat box turns into a Stop button. Stop (or
any other click) ends this script run; the stream is closed, the API sees
the client leave and stops the agents, and the next run marks the turn
as stopped.

Streamlit runs this file from top to bottom on every click, so all state
lives in `st.session_state`: the conversations of this browser tab.
"""

from contextlib import closing

import streamlit as st
from streamlit.delta_generator import DeltaGenerator

from ui import views
from ui.client import ApiError, ChatClient, StreamOpened
from ui.config import MAX_QUESTION_CHARS, ROLES, UiSettings
from ui.conversation import Conversation, Turn, TurnStatus
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


# ---------------------------------------------------------------
# State: the conversations of this browser tab
# ---------------------------------------------------------------


def _conversations() -> list[Conversation]:
    state = st.session_state
    if "conversations" not in state:
        first = Conversation(role=state.get("role", ROLES[0]))
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


# Callbacks run before the page is drawn again, so they may change the
# role picker's value.


def _new_conversation() -> None:
    current = _current()
    if not current.turns:  # already empty: just use it with the chosen role
        current.role = st.session_state.role
        return
    fresh = Conversation(role=st.session_state.role)
    st.session_state.conversations.append(fresh)
    st.session_state.current = fresh.key


def _open(key: str) -> None:
    st.session_state.current = key
    st.session_state.role = _current().role


def _ask_example(question: str) -> None:
    st.session_state.pending = question


# ---------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------


@st.cache_data(ttl=10, show_spinner=False)
def _api_is_up(api_url: str) -> bool:
    return ChatClient(UiSettings(api_url=api_url)).is_up()


def _sidebar(settings: UiSettings, current: Conversation) -> None:
    with st.sidebar:
        st.button(
            "New conversation",
            icon=":material/add:",
            on_click=_new_conversation,
            width="stretch",
        )
        st.selectbox(
            "Role",
            ROLES,
            key="role",
            on_change=_new_conversation,
            help=(
                "Until sign-in is added, pick the role to ask as. The API decides "
                "what each role may read. Changing it starts a new conversation."
            ),
        )

        st.markdown("**Conversations**")
        for conversation in reversed(_conversations()):
            st.button(
                conversation.title,
                key=f"open-{conversation.key}",
                on_click=_open,
                args=(conversation.key,),
                type="primary" if conversation.key == current.key else "secondary",
                help=f"Role: {conversation.role}",
                width="stretch",
            )
        st.caption("Conversations are kept while this tab is open.")

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
    conversation: Conversation,
    question: str,
    answer_slot: DeltaGenerator,
    panel_slot: DeltaGenerator,
) -> None:
    turn = Turn(question=question)
    conversation.turns.append(turn)

    def draw_answer() -> None:
        with answer_slot.container():
            views.answer(turn)

    def draw_all() -> None:
        draw_answer()
        with panel_slot.container():
            views.activity(turn)

    draw_all()
    try:
        stream = client.ask(question, role=conversation.role, thread_id=conversation.thread_id)
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
    draw_all()


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

    # A turn still marked as streaming belongs to a run that was stopped:
    # the user pressed Stop, or clicked something while it streamed.
    for conversation in _conversations():
        for turn in conversation.turns:
            if turn.status is TurnStatus.STREAMING:
                turn.stop()

    current = _current()
    _sidebar(settings, current)

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
        _ask(client, current, str(question), answer_slot, panel_slot)
        # Draw the page again from the saved state: history, panel picker
        # and an enabled chat box.
        st.rerun()


main()
