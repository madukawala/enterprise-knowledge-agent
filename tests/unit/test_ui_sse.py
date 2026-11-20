"""The UI's reader for server-sent events."""

from ui.sse import KeepAlive, SseMessage, read_sse


def test_a_message_has_its_event_id_and_data() -> None:
    lines = ["id: 3", "event: token", 'data: {"text": "Hi"}', ""]
    assert list(read_sse(lines)) == [SseMessage(event="token", data='{"text": "Hi"}', id="3")]


def test_several_data_lines_are_joined_with_new_lines() -> None:
    (message,) = read_sse(["data: first", "data: second", ""])
    assert message == SseMessage(event="message", data="first\nsecond")


def test_comment_lines_are_keep_alives() -> None:
    items = list(read_sse([": keep-alive", "", "event: token", "data: x", ""]))
    assert items == [KeepAlive(), SseMessage(event="token", data="x")]


def test_only_one_space_after_the_colon_is_removed_and_line_endings_are_ignored() -> None:
    (message,) = read_sse(["event:token\r\n", "data:  two spaces\r\n", "\r\n"])
    assert message == SseMessage(event="token", data=" two spaces")


def test_messages_without_data_and_unknown_fields_are_skipped() -> None:
    assert list(read_sse(["event: token", "", "retry: 1000", ""])) == []


def test_the_event_name_does_not_carry_over_to_the_next_message() -> None:
    first, second = read_sse(["event: token", "data: a", "", "data: b", ""])
    assert (first.event, second.event) == ("token", "message")  # type: ignore[union-attr]


def test_an_unfinished_message_at_the_end_is_dropped() -> None:
    # The connection closed before the empty line that ends the message.
    assert list(read_sse(["data: complete", "", "event: token", "data: half"])) == [
        SseMessage(event="message", data="complete")
    ]
