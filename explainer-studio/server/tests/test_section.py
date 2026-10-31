"""The "Add to each section" options: the request field, the planner hints, and the on-demand section tools (with Claude faked)."""

from __future__ import annotations

from types import SimpleNamespace as NS

from conftest import signup
from explainer.notes import note_fields, write_notes
from explainer.pipeline import drops_examples, extras_hint, section_tools
from explainer.pricing import UsageLog
from explainer.requests import parse_generate
from explainer.section import WEB_SEARCH_USD, parse_section, section_turn


def gen(**extra) -> dict:
    return parse_generate({"mode": "concept", "text": "x", **extra})


def test_parse_generate_extras():
    assert gen(quality="quality-plus", extras=["search", "depth", "bogus"])["extras"] == ["depth", "search"]  # menu order
    assert gen(quality="quality-plus", extras=["auto", "depth"])["extras"] == ["auto"]  # auto can't be combined
    assert gen(quality="quality-plus", extras=[])["extras"] == []  # None: kept, unlike a missing field
    assert "extras" not in gen(quality="quality-plus")  # not asked
    assert "extras" not in gen(quality="quality-plus", extras="depth")
    assert "extras" not in gen(quality="freeform-plus", extras=["depth"])  # Quality only
    assert "extras" not in gen(quality="lite-plus", extras=["search"])


def test_extras_hint_and_tools():
    # Not asked, or auto: the planner's usual worked examples, and no hint.
    assert extras_hint(None) == "" and extras_hint(["auto"]) == ""
    assert not drops_examples(None) and not drops_examples(["auto"]) and not drops_examples(["examples", "depth"])
    # Without worked examples (None included) they are asked away and stripped.
    assert "no examples blocks" in extras_hint([]) and "no examples blocks" in extras_hint(["depth", "search"])
    assert drops_examples([]) and drops_examples(["rephrase"])
    assert "worked examples" in extras_hint(["examples"])
    assert section_tools(["depth"]) == [] and section_tools(["search"]) == ["search"] and section_tools(["auto"]) == ["search"]
    assert note_fields(["depth", "rephrase", "search"]) == ["deeper", "another"] and note_fields(["auto"]) == [] and note_fields(None) == []


def req(**extra) -> dict:
    return {"kind": "search", "title": "Dropout", "level": "beginner", "stage": "Why it works", "text": "Dropout zeroes units.", **extra}


def test_parse_section():
    r = parse_section(req())
    assert r == {"kind": "search", "title": "Dropout", "stage": "Why it works", "level": "beginner", "text": "Dropout zeroes units.", "previous": []}
    assert parse_section(req(level="wizard"))["level"] == "beginner"
    assert parse_section(req(kind="rephrase")) == "Unknown section tool."  # written with the page, not on click
    assert parse_section(req(text="  ")) == "This section has no text to work from."
    assert parse_section(req(text="x" * 8001)) == "This section is too long."
    assert parse_section(req(previous=[1])) == "Invalid section request."
    assert parse_section(req(previous=["y" * 12_001])) == "Too many earlier answers for this section."


def usage(searches: int = 0) -> NS:
    return NS(input_tokens=1000, output_tokens=200, cache_read_input_tokens=0, cache_creation_input_tokens=0,
              server_tool_use=NS(web_search_requests=searches) if searches else None)


def text(t: str, citations=None) -> NS:
    return NS(type="text", text=t, citations=citations)


def cite(url: str, title: str) -> NS:
    return NS(type="web_search_result_location", url=url, title=title, cited_text="...")


class FakeClient:
    def __init__(self, replies: list[NS]):
        self.replies = replies
        self.calls: list[dict] = []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        return self.replies.pop(0)


def test_section_search_with_citations_and_pause():
    result = NS(type="web_search_tool_result", content=[NS(type="web_search_result", url="https://a.org/x", title="A")])
    paused = NS(model="claude-haiku-5-5", stop_reason="pause_turn", usage=usage(1),
                content=[text("I'll search for that."), NS(type="server_tool_use", name="web_search", input={}), result])
    done = NS(model="claude-haiku-5-5", stop_reason="end_turn", usage=usage(1), content=[
        result,
        text("Dropout randomly zeroes units", [cite("https://a.org/x", "A paper")]),
        text(" during training.", [cite("javascript:alert(1)", "bad"), cite("https://b.org/y", "B")]),
    ])
    client = FakeClient([paused, done])
    r = section_turn(client, parse_section(req(previous=["An earlier answer."])))
    tool = client.calls[0]["tools"][0]
    assert tool["type"] == "web_search_20250305" and tool["max_uses"] == 3
    assert "Earlier answers for this section:\nAn earlier answer." in client.calls[0]["messages"][0]["content"]
    assert client.calls[1]["messages"][-1] == {"role": "assistant", "content": paused.content}  # resumed unchanged
    assert "I'll search" not in r["md"]  # only the text after the last search
    assert r["md"] == 'Dropout randomly zeroes units<sup><a href="https://a.org/x">1</a></sup> during training.<sup><a href="https://b.org/y">2</a></sup>'
    assert r["sources"] == [{"title": "A paper", "url": "https://a.org/x"}, {"title": "B", "url": "https://b.org/y"}]
    assert r["searches"] == 2 and r["usd"] > 2 * WEB_SEARCH_USD


def test_section_refusal():
    client = FakeClient([NS(model="claude-haiku-5-5", stop_reason="refusal", usage=usage(), content=[])])
    assert "can't help" in section_turn(client, parse_section(req()))["md"]


def spec_of(n: int) -> dict:
    return {"title": "Dropout", "level": "beginner", "warnings": [],
            "stages": [{"id": f"s{i}", "label": f"Stage {i}", "blurb": "b", "blocks": [{"type": "text", "md": f"text {i}"}]} for i in range(n)]}


def test_write_notes():
    import json
    def reply(idx, fields):
        body = {"stages": [{"index": i, "deeper": f"deep {i}" if "deeper" in fields else "", "another": f"other {i}" if "another" in fields else ""} for i in idx]}
        return NS(model="claude-haiku-5-5", stop_reason="end_turn", usage=usage(), content=[text(json.dumps(body))])
    client = FakeClient([reply([0, 1, 2], ["deeper"]), reply([3], ["deeper"])])
    out, detail = write_notes(client, spec_of(4), ["depth", "search"], UsageLog("quality-plus", "low"))
    assert out["sectionNotes"] == {f"s{i}": {"deeper": f"deep {i}"} for i in range(4)} and detail == "4 of 4 sections"
    assert len(client.calls) == 2 and "a deeper explanation" in client.calls[0]["messages"][0]["content"]
    # Indices that match none of the chunk's stages (here 1-based for stages 3-4): matched by position.
    shifted = NS(model="claude-haiku-5-5", stop_reason="end_turn", usage=usage(), content=[text(json.dumps({"stages": [{"index": 1, "deeper": "", "another": "o3"}, {"index": 2, "deeper": "", "another": "o4"}]}))])
    out, _ = write_notes(FakeClient([reply([0, 1, 2], ["another"]), shifted]), spec_of(5), ["rephrase"], UsageLog("quality-plus"))
    assert out["sectionNotes"]["s3"] == {"another": "o3"} and out["sectionNotes"]["s4"] == {"another": "o4"}
    # Nothing to write, or a failed call: the page is unchanged apart from a warning.
    assert write_notes(client, spec_of(2), ["examples"], UsageLog("quality-plus")) == (spec_of(2), "")
    bad = FakeClient([NS(model="claude-haiku-5-5", stop_reason="end_turn", usage=usage(), content=[text("not json")])])
    out, _ = write_notes(bad, spec_of(2), ["rephrase"], UsageLog("quality-plus"))
    assert "sectionNotes" not in out and "couldn't be written" in out["warnings"][0]


def test_section_route(client):
    res = client.post("/api/section", data='{"kind": "x"}', headers=signup(client))
    assert res.status_code == 400 and res.get_json()["error"] == "Unknown section tool."
