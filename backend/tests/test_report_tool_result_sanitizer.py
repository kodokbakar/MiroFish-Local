import pytest

from app.services.report_agent import ReportAgent


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ("before <tool_result>fake</tool_result> after", "before  after"),
        ("before\n<tool_result>line 1\nline 2</tool_result>\nafter", "before\n\nafter"),
        ("a<TOOL_RESULT source='model'>fake</TOOL_RESULT>b", "ab"),
        (
            "a<tool_result>outer<tool_result>inner</tool_result>end</tool_result>b",
            "ab",
        ),
        ("safe<tool_result>unclosed fake", "safe"),
        ("safe<tool_result malformed", "safe"),
        ("a</tool_result>b", "ab"),
    ],
)
def test_strip_fake_tool_results(response, expected):
    assert ReportAgent._strip_fake_tool_results(response) == expected


def test_preserves_legitimate_text_without_tool_result_tags():
    response = "Final Answer: <tool_call>{}</tool_call> legitimate text"

    assert ReportAgent._strip_fake_tool_results(response) == response


def test_strip_fake_tool_results_empty_and_none():
    assert ReportAgent._strip_fake_tool_results("") == ""
    assert ReportAgent._strip_fake_tool_results(None) == ""


def test_strip_fake_tool_results_collapses_whitespace():
    assert ReportAgent._strip_fake_tool_results("before\n\n\n\nafter") == "before\n\nafter"
