from mlrun.utils.helpers import verify_field_regex, extend_hub_uri
from mlrun.utils.regex import run_name


def test_run_name_regex():
    cases = [
        {"value": "asd", "valid": True},
        {"value": "Asd", "valid": True},
        {"value": "AsA", "valid": True},
        {"value": "As-123_2.8A", "valid": True},
        {"value": "1As-123_2.8A5", "valid": True},
        {
            "value": "azsxdcfvg-azsxdcfvg-azsxdcfvg-azsxdcfvg-azsxdcfvg-azsxdcfvg-azs",
            "valid": True,
        },
        {
            # Invalid because the first letter is -
            "value": "-As-123_2.8A",
            "valid": False,
        },
        {
            # Invalid because the last letter is .
            "value": "As-123_2.8A.",
            "valid": False,
        },
        {
            # Invalid because $ is not allowed
            "value": "As-123_2.8A$a",
            "valid": False,
        },
        {
            # Invalid because it's more then 63 characters
            "value": "azsxdcfvg-azsxdcfvg-azsxdcfvg-azsxdcfvg-azsxdcfvg-azsxdcfvg-azsx",
            "valid": False,
        },
    ]
    for case in cases:
        try:
            verify_field_regex("test_field", case["value"], run_name)
        except Exception:
            if case["valid"]:
                raise


def test_extend_hub_uri():
    cases = [
        {
            "input": "http://no-hub-prefix",
            "expected_output": 'http://no-hub-prefix',
        },
        {
            "input": "hub://function_name",
            "expected_output": 'https://raw.githubusercontent.com/mlrun/functions/master/function_name/function.yaml',
        },
        {
            "input": "hub://function_name:development",
            "expected_output": 'https://raw.githubusercontent.com/mlrun/functions/development/function_name/function.yaml',
        },
        {
            "input": "hub://function-name",
            "expected_output": 'https://raw.githubusercontent.com/mlrun/functions/master/function_name/function.yaml',
        },
        {
            "input": "hub://function-name:development",
            "expected_output": 'https://raw.githubusercontent.com/mlrun/functions/development/function_name/function.yaml',
        },
    ]
    for case in cases:
        input = case['input']
        expected_output = case['expected_output']
        output = extend_hub_uri(input)
        assert expected_output == output
