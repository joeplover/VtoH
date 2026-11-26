from server.caption_format import caption_lines


def test_caption_lines_keep_text_and_prefer_punctuation():
    text = "这是第一段完整的字幕。" * 4 + "这是下一段，需要继续阅读。" * 6
    lines = caption_lines(text)
    assert "".join(lines) == text
    assert all(len(line) <= 68 for line in lines)
    assert lines[0].endswith("。")


def test_caption_lines_keep_short_text_and_avoid_breaking_english_words():
    assert caption_lines("A short caption.") == ["A short caption."]
    text = "The speaker continues without sentence punctuation " * 5
    lines = caption_lines(text)
    assert "".join(lines) == text
    assert all(line.endswith(" ") for line in lines[:-1])
