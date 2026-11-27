"""The public profile: the only knowledge the public tier can ever return.

It is a small markdown document the owner writes by hand. Each `## Heading`
starts a section; answers are whole sections, ranked by word overlap with the
question. No embeddings, no model, no corpus access: what isn't in this file
cannot leak through the public tier.
"""

import math
import re

_WORD = re.compile(r"[a-z0-9][a-z0-9+#.-]*")
_STOP = frozenset(
    "a an and are as at be but by can could did do does for from has have he her his how i if in "
    "into is it its me my of on or our she so that the their them they this to was we what when "
    "where which why will with would you your about tell know any some".split()
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

    def answer(self, question: str, k: int = 3, fallback: bool = True) -> list[dict]:
        """Top-k sections for the question. With `fallback`, no match returns the first section."""
        q = _tokens(question or "")
        n = len(self.sections)
        # Rare words carry the signal: the owner's name appears everywhere and
        # should not decide the answer. Title hits count double.
        idf = {t: math.log(1 + n / (1 + sum(t in s["_tokens"] for s in self.sections))) for t in q}
        scored = []
        for i, s in enumerate(self.sections):
            score = sum(idf[t] for t in q & s["_tokens"]) + sum(idf[t] for t in q & s["_title"])
            if score:
                scored.append((score, -i, s))
        scored.sort(reverse=True)
        picked = [s for _, _, s in scored[:k]] or (self.sections[:1] if fallback else [])
        return [{"title": s["title"], "text": s["text"]} for s in picked]
