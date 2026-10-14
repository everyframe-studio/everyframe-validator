from everyframe_validator.cli import parser


def test_operator_help_uses_current_brand_and_preserves_subnet_default():
    help_text = parser().format_help()
    assert "EveryFrame independent validator" in help_text
    assert "Everyframe" not in help_text
    assert "Mainnet SN117; dry-run by default" in help_text
