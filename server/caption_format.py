from unicodedata import east_asian_width


def caption_lines(text: str) -> list[str]:
    """Break long captions at readable boundaries while preserving their text."""
    lines: list[str] = []
    start = 0
    while start < len(text):
        end = start
        width = 0
        sentence = clause = word = None
        while end < len(text) and width < 67:
            char = text[end]
            end += 1
            width += 2 if east_asian_width(char) in ("W", "F") else 1
            if char == "\n":
                break
            if width < 26:
                continue
            if char in "。！？!?；;" or (char == "." and text[end:end + 1].isspace()):
                sentence = end
            elif char in "，、：,:":
                clause = end
            elif char.isspace():
                word = end
        if end < len(text) and text[end - 1] != "\n":
            end = sentence or clause or word or end
        lines.append(text[start:end])
        start = end
    return lines
