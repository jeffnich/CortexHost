"""The public profile: the only knowledge the public tier can ever return.

It is a small markdown document the owner writes by hand. Each `## Heading`
starts a section; answers are whole sections, ranked by word overlap with the
question. No embeddings, no model, no corpus access: what isn't in this file
cannot leak through the public tier.
"""

import re

_WORD = re.compile(r"[a-z0-9][a-z0-9+#.-]*")
_STOP = frozenset(
    "a an and are as at be but by can could did do does for from has have he her his how i if in "
    "into is it its me my of on or our she so that the their them they this to was we what when "
    "where which who why will with would you your about tell know any some".split()
)


def _tokens(text: str) -> set[str]:
    out = set()
    for w in _WORD.findall(text.lower()):
        w = w.strip(".-")
        if len(w) < 2 or w in _STOP:
            continue
        out.add(w)
        if len(w) > 4 and w.endswith("s"):
            out.add(w[:-1])  # cheap plural folding: projects -> project
    return out


class Profile:
    def __init__(self, markdown: str):
        self.sections: list[dict] = []
        title, buf = "About", []
        markdown = re.sub(r"<!--.*?-->", "", markdown or "", flags=re.S)  # author notes stay private
        for line in markdown.splitlines():
            m = re.match(r"^##\s+(.+?)\s*$", line)
            if m:
                self._add(title, buf)
                title, buf = m.group(1), []
            elif not line.startswith("# "):  # drop the document title
                buf.append(line)
        self._add(title, buf)

    def _add(self, title: str, lines: list[str]) -> None:
        body = "\n".join(lines).strip()
        if body:
            self.sections.append(
                {"title": title, "text": body, "_tokens": _tokens(title) | _tokens(body), "_title": _tokens(title)}
            )

    @property
    def empty(self) -> bool:
        return not self.sections

    def topics(self) -> list[str]:
        return [s["title"] for s in self.sections]

    def answer(self, question: str, k: int = 3) -> list[dict]:
        """Top-k sections for the question. Falls back to the first section."""
        q = _tokens(question or "")
        scored = []
        for i, s in enumerate(self.sections):
            # Title hits count double: "projects" should land on "## Projects".
            score = len(q & s["_tokens"]) + len(q & s["_title"])
            if score:
                scored.append((score, -i, s))
        scored.sort(reverse=True)
        picked = [s for _, _, s in scored[:k]] or self.sections[:1]
        return [{"title": s["title"], "text": s["text"]} for s in picked]
