"""Reads a server-sent event stream, one line at a time.

Only the parts of the format the API uses are handled: the `event`, `id`
and `data` fields, and comment lines such as `: keep-alive`. A message
ends with an empty line. The full format is at
https://html.spec.whatwg.org/multipage/server-sent-events.html

    for item in read_sse(response.iter_lines()):
        if isinstance(item, KeepAlive):
            ...  # the API is still working; nothing new yet
        else:
            item.event, item.data
"""

from collections.abc import Iterable, Iterator
from dataclasses import dataclass


@dataclass(frozen=True)
class SseMessage:
    event: str
    data: str
    id: str | None = None


@dataclass(frozen=True)
class KeepAlive:
    """A comment line. It only tells the client that the connection is alive."""


def read_sse(lines: Iterable[str]) -> Iterator[SseMessage | KeepAlive]:
    event = ""
    data: list[str] = []
    last_id: str | None = None

    for raw in lines:
        line = raw.rstrip("\r\n")
        if not line:
            # An empty line ends the message. One without data is skipped,
            # as the format says.
            if data:
                yield SseMessage(event=event or "message", data="\n".join(data), id=last_id)
            event, data = "", []
            continue
        if line.startswith(":"):
            yield KeepAlive()
            continue

        name, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if name == "data":
            data.append(value)
        elif name == "event":
            event = value
        elif name == "id":
            last_id = value
        # Other fields (such as `retry`) are not used by the API.

    # Lines after the last empty line are an unfinished message: the
    # connection dropped half-way, so it is not passed on.
